import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

BOT_DIR = Path(__file__).resolve().parents[1] / "node/opt/homelab-telegram-bot"


@pytest.fixture
def bot_modules(monkeypatch):
    modules = {}
    for name in ("router", "executors", "bot"):
        spec = importlib.util.spec_from_file_location(name, BOT_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    return SimpleNamespace(**modules)


def make_router(modules, logs):
    router = modules.router.Router(privileged_users=set(), log=logs.append)
    modules.executors.register_all(router, allowed_units=set(), log=logs.append)
    return router


def stub_http(monkeypatch, modules, reply):
    calls = []

    def open_response(request, *, timeout):
        calls.append((request, timeout))
        return io.BytesIO(json.dumps(reply).encode())

    monkeypatch.setattr(modules.executors.urllib.request, "urlopen", open_response)
    return calls


def test_ask_answer_with_numbered_source_titles(bot_modules, monkeypatch):
    calls = stub_http(
        monkeypatch,
        bot_modules,
        {
            "answer": "Fixture answer [2], then [1].",
            "refused": False,
            "sources": [{"number": 2, "title": "Second"}, {"number": 1, "title": "First"}],
        },
    )
    logs = []
    router = make_router(bot_modules, logs)
    reply = router.dispatch(0, "/ask Private fixture question")
    assert reply == "Fixture answer [2], then [1].\n\nSources:\n[2] Second\n[1] First"
    request, timeout = calls[0]
    assert len(calls) == 1
    assert request.full_url == "http://127.0.0.1:8000/v1/knowledge/answer"
    assert request.method == "POST"
    assert json.loads(request.data) == {"question": "Private fixture question"}
    assert request.get_header("Content-type") == "application/json"
    assert timeout == 90
    assert "Private fixture question" not in "\n".join(logs)


def test_ask_refusal_has_no_sources(bot_modules, monkeypatch):
    stub_http(
        monkeypatch,
        bot_modules,
        {
            "answer": "These sources do not answer the question.",
            "refused": True,
            "sources": [{"number": 1, "title": "Must not appear"}],
        },
    )
    assert bot_modules.executors._ask(["Neutral"]) == "These sources do not answer the question."


def test_api_down_keeps_other_commands_working(bot_modules, monkeypatch):
    def offline(*args, **kwargs):
        raise urllib.error.URLError("Private fixture question")

    monkeypatch.setattr(bot_modules.executors.urllib.request, "urlopen", offline)
    monkeypatch.setattr(bot_modules.executors, "_status", lambda args: "host: fixture")
    router = make_router(bot_modules, [])
    assert "knowledge service is down" in router.dispatch(0, "/ask Neutral")
    assert router.dispatch(0, "/status") == "host: fixture"
    assert router.dispatch(0, "/spend") == "Unknown command. Try /help"
    assert router.dispatch(0, "/model") == "Unknown command. Try /help"
    assert "/ask" in router.dispatch(0, "/help")
    assert router.dispatch(0, "/start") == router.dispatch(0, "/help")


def test_model_output_remains_text_and_privileged_gate_remains(bot_modules, monkeypatch):
    stub_http(
        monkeypatch,
        bot_modules,
        {
            "answer": "/restart chrony.service [1]",
            "refused": False,
            "sources": [{"number": 1, "title": "Fixture"}],
        },
    )

    def must_not_run(*args, **kwargs):
        pytest.fail("model output reached a subprocess")

    monkeypatch.setattr(bot_modules.executors.subprocess, "run", must_not_run)
    router = make_router(bot_modules, [])
    assert router.dispatch(0, "/ask Neutral").startswith("/restart chrony.service [1]")
    assert "not authorised" in router.dispatch(0, "/restart chrony.service")


@pytest.mark.parametrize("failure", [None, "not-ok", "exception"])
def test_startup_registers_profile_from_router_and_failure_continues(
    bot_modules, monkeypatch, failure
):
    bot = bot_modules.bot
    monkeypatch.setattr(bot, "load_token", lambda: "")
    monkeypatch.setattr(bot, "load_allowlist", lambda: {0})
    monkeypatch.setattr(bot, "load_privileged", set)
    monkeypatch.setattr(bot, "load_restart_units", set)
    calls, polls, logs = [], [], []
    monkeypatch.setattr(bot, "log", logs.append)

    def api_call(method, params):
        calls.append((method, params))
        if failure == "exception":
            raise RuntimeError("Private exception text")
        return {"ok": failure != "not-ok"}

    monkeypatch.setattr(bot, "api_call", api_call)
    monkeypatch.setattr(bot, "poll_forever", lambda allowlist, router: polls.append(router))
    bot.main()
    assert len(polls) == 1
    assert [method for method, _ in calls] == ["setMyCommands", "setMyDescription"]
    commands = json.loads(calls[0][1]["commands"])
    assert {command["command"] for command in commands} == {
        "status",
        "disk",
        "uptime",
        "restart",
        "ask",
        "help",
        "start",
    }
    assert commands == [
        {"command": name[1:], "description": executor.summary}
        for name, executor in polls[0].executors.items()
    ]
    description = calls[1][1]["description"]
    assert len(description) <= 512
    assert all(
        f"/{command['command']}: {command['description']}" in description for command in commands
    )
    assert "Private exception text" not in "\n".join(logs)
    assert all(
        ("succeeded" if failure is None else "WARNING") in line
        for line in logs
        if "command registration" in line
    )


def test_handler_error_logs_command_and_type_without_content(bot_modules, monkeypatch):
    logs = []
    question = "Private fixture question"

    def failing_handler(args):
        raise ValueError(question)

    monkeypatch.setattr(bot_modules.executors, "_ask", failing_handler)
    monkeypatch.setattr(bot_modules.bot, "log", logs.append)
    router = make_router(bot_modules, logs)
    reply = bot_modules.bot.dispatch_reply(router, 0, f"/ask {question}")
    assert reply == "That command failed. The error is in the journal."
    assert logs[-1] == "ERROR: command /ask failed for user 0: ValueError"
    assert question not in "\n".join(logs)


def test_long_replies_sent_in_order_within_telegram_limit(bot_modules, monkeypatch):
    calls = []
    text = "Fixture [1]\n" + "😀" * 5000 + "\nSources:\n[1] Fixture title"

    def api_call(method, params, *, timeout):
        calls.append((method, params, timeout))
        return {"ok": True}

    monkeypatch.setattr(bot_modules.bot, "api_call", api_call)
    bot_modules.bot.send_message(0, text)
    assert len(calls) > 1
    assert "".join(params["text"] for _, params, _ in calls) == text
    assert all(len(params["text"].encode("utf-16-le")) // 2 <= 4096 for _, params, _ in calls)
    assert all(
        method == "sendMessage" and params["chat_id"] == 0 and timeout == 30
        for method, params, timeout in calls
    )
