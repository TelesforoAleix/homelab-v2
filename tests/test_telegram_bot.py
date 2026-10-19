import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram import Update
from telegram.request import BaseRequest

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


class TelegramRequest(BaseRequest):
    """Exercise the real Application/Bot serializers without contacting Telegram."""

    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    @property
    def read_timeout(self):
        return 15

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        name = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((name, params))
        if name == "getMe":
            result = {"id": 123, "is_bot": True, "first_name": "Fixture", "username": "fixture_bot"}
        elif name in {"setMyCommands", "setMyDescription"}:
            if self.failure == "exception":
                raise RuntimeError("Private exception text")
            result = self.failure != "not-ok"
        elif name == "sendMessage":
            result = {
                "message_id": 1,
                "date": 0,
                "chat": {"id": params["chat_id"], "type": "private"},
                "text": params["text"],
            }
        else:
            result = []
        return 200, json.dumps({"ok": True, "result": result}).encode()


@pytest.fixture
async def telegram_app(bot_modules, monkeypatch):
    from telegram.ext import ExtBot

    request = TelegramRequest()
    monkeypatch.setattr(
        bot_modules.bot,
        "AuditedBot",
        lambda token: ExtBot(token, request=request, get_updates_request=request),
    )
    logs = []
    monkeypatch.setattr(bot_modules.bot, "log", logs.append)
    router = bot_modules.router.Router(privileged_users={1}, log=logs.append)
    bot_modules.executors.register_all(router, allowed_units={"chrony.service"}, log=logs.append)
    app = bot_modules.bot.build_application("123:fixture", {0, 1}, router)
    async with app:
        yield app, request, router, logs


async def deliver(app, text, user=0, *, edited=False):
    update = Update.de_json(
        {
            "update_id": 1,
            "edited_message" if edited else "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": 10, "type": "private"},
                "from": {"id": user, "is_bot": False, "first_name": "Fixture"},
                **({"text": text} if text is not None else {}),
            },
        },
        app.bot,
    )
    await app.process_update(update)


@pytest.mark.parametrize("failure", [None, "not-ok", "exception"])
async def test_startup_registers_profile_from_router_and_failure_continues(telegram_app, failure):
    app, request, router, logs = telegram_app
    request.failure = failure
    await app.post_init(app)
    await deliver(app, "/help")
    calls = dict(request.calls)
    commands = calls["setMyCommands"]["commands"]
    assert commands == [
        {"command": name[1:], "description": executor.summary}
        for name, executor in router.executors.items()
    ]
    assert {c["command"] for c in commands} == {
        "status",
        "disk",
        "uptime",
        "restart",
        "backup",
        "ask",
        "help",
        "start",
    }
    description = calls["setMyDescription"]["description"]
    assert len(description) <= 512
    assert all(f"/{c['command']}: {c['description']}" in description for c in commands)
    assert "Home Lab bot" in calls["sendMessage"]["text"]
    assert "Private exception text" not in "\n".join(logs)
    registration = [line for line in logs if "command registration" in line]
    assert len(registration) == 2
    assert all(("succeeded" if failure is None else "WARNING") in line for line in registration)


async def test_library_dispatch_gates_and_locked_volume(telegram_app, bot_modules, monkeypatch):
    app, request, router, logs = telegram_app

    def offline(*args, **kwargs):
        raise urllib.error.URLError("private question")

    monkeypatch.setattr(bot_modules.executors.urllib.request, "urlopen", offline)
    subprocess = []
    monkeypatch.setattr(
        bot_modules.executors.subprocess, "run", lambda *a, **k: subprocess.append(a)
    )
    for text, user, expected in (
        ("/status", 99, "Not authorised."),
        ("/restart chrony", 0, "not authorised"),
        ("/backup", 0, "not authorised"),
        ("/restart ssh", 1, "is not permitted"),
        ("/ask private question", 0, "knowledge service is down"),
        (None, 0, "Unknown command"),
        ("/help@fixture_bot", 0, "Home Lab bot"),
        ("/start", 0, "Home Lab bot"),
    ):
        await deliver(app, text, user, edited=True)
        assert expected in request.calls[-1][1]["text"]
    assert not subprocess
    assert "private question" not in "\n".join(logs)


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


async def test_long_replies_sent_in_order_within_telegram_limit(telegram_app, bot_modules):
    app, request, _, _ = telegram_app
    text = "Fixture [1]\n" + "😀" * 5000 + "\nSources:\n[1] Fixture title"
    await bot_modules.bot.send_message(app.bot, 10, text)
    calls = [params for method, params in request.calls if method == "sendMessage"]
    assert len(calls) > 1
    assert "".join(params["text"] for params in calls) == text
    assert all(len(params["text"].encode("utf-16-le")) // 2 <= 4096 for params in calls)
    assert all(params["chat_id"] == 10 for params in calls)


@pytest.mark.parametrize("code", [302, 404, 500])
def test_backup_http_errors_are_failed_without_second_call(bot_modules, backup_listener, code):
    backup_listener.code = code
    backup_listener.reply = {"status": "done", "private": "must not appear"}
    logs = []
    assert bot_modules.executors.make_backup(logs.append).handler([], 0) == (
        "Copy failed: listener request failed."
    )
    assert backup_listener.calls == [("/backup", b"")]
    assert "must not appear" not in "\n".join(logs)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://localhost:8091",
        "http://localhost:8091/backup",
        "http://localhost:8091?args=x",
        "http://localhost:8090",
    ],
)
def test_backup_invalid_configuration_never_calls(bot_modules, monkeypatch, tmp_path, url):
    config = tmp_path / "url"
    config.write_text(url)
    monkeypatch.setattr(bot_modules.executors, "MAC_LISTENER_URL", config)
    monkeypatch.setattr(
        bot_modules.executors.urllib.request,
        "build_opener",
        lambda *a: pytest.fail("invalid configuration reached HTTP"),
    )
    assert bot_modules.executors.make_backup(lambda s: None).handler([], 0) == (
        "Copy failed: invalid listener response or configuration."
    )


@pytest.fixture
def backup_listener(bot_modules, monkeypatch, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = SimpleNamespace(reply={"status": "no-card"}, code=200, calls=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state.calls.append((self.path, body))
            self.send_response(state.code)
            self.end_headers()
            self.wfile.write(json.dumps(state.reply).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 8091), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    config = tmp_path / "mac-listener-url"
    config.write_text("http://127.0.0.1:8091\n")
    monkeypatch.setattr(bot_modules.executors, "MAC_LISTENER_URL", config)
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize(
    "response,code,expected,result,count",
    [
        ({"status": "no-card"}, 200, "Connect the SD card and send /backup again.", "no-card", 0),
        (
            {"status": "done", "file": "homelab-originals-2026-10-08.tar.age", "bytes": 307200},
            200,
            "Copy done: 300 KB, homelab-originals-2026-10-08.tar.age.",
            "done",
            307200,
        ),
        (
            {"status": "failed", "reason": "copy timed out"},
            200,
            "Copy failed: copy timed out.",
            "failed",
            0,
        ),
        ({"status": "busy"}, 409, "Copy failed: a copy is already running.", "busy", 0),
    ],
)
def test_backup_replies_from_stub_listener(
    bot_modules, backup_listener, response, code, expected, result, count
):
    backup_listener.reply, backup_listener.code = response, code
    logs = []
    router = bot_modules.router.Router(privileged_users={0}, log=logs.append)
    bot_modules.executors.register_all(router, allowed_units=set(), log=logs.append)
    assert router.dispatch(0, "/backup") == expected
    assert backup_listener.calls == [("/backup", b"")]
    assert logs[-1] == f"backup requester=0 result={result} bytes={count}"
    assert "homelab-originals" not in "\n".join(logs)


@pytest.mark.parametrize(
    "response",
    [
        {},
        [],
        {"status": "arbitrary"},
        {"status": "failed", "reason": "Private fixture content"},
        {"status": "done", "bytes": True, "file": "homelab-originals-2026-10-08.tar.age"},
        {"status": "done", "bytes": 12, "file": "../../private"},
        {"status": "done", "bytes": -1, "file": "homelab-originals-2026-10-08.tar.age"},
    ],
)
def test_backup_invalid_responses_do_not_echo_content(bot_modules, backup_listener, response):
    backup_listener.reply = response
    logs = []
    executor = bot_modules.executors.make_backup(logs.append)
    assert executor.handler([], 0) == "Copy failed: invalid listener response or configuration."
    assert "Private fixture content" not in "\n".join(logs)


@pytest.mark.parametrize(
    "failure", [ConnectionRefusedError(), TimeoutError(), urllib.error.URLError("private")]
)
def test_backup_unreachable_and_timeout(bot_modules, tmp_path, monkeypatch, failure):
    config = tmp_path / "url"
    config.write_text("http://127.0.0.1:8091")
    monkeypatch.setattr(bot_modules.executors, "MAC_LISTENER_URL", config)
    calls = []

    class Opener:
        def open(self, req, timeout):
            calls.append((req, timeout))
            raise failure

    monkeypatch.setattr(bot_modules.executors.urllib.request, "build_opener", lambda *a: Opener())
    logs = []
    assert bot_modules.executors.make_backup(logs.append).handler([], 0) == (
        "The Mac is not reachable; wake it and send /backup again."
    )
    assert len(calls) == 1
    assert calls[0][1] == 210
    assert "private" not in "\n".join(logs)


def test_backup_missing_config_and_argument_refusal(bot_modules, tmp_path, monkeypatch):
    monkeypatch.setattr(bot_modules.executors, "MAC_LISTENER_URL", tmp_path / "absent")
    executor = bot_modules.executors.make_backup(lambda s: None)
    assert executor.handler([], 0) == "Backup is not configured."
    assert executor.handler(["private argument"], 0) == "Usage: /backup"


def test_backup_privilege_gate_prevents_call_and_argument_logging(bot_modules, monkeypatch):
    def must_not_call(*args, **kwargs):
        pytest.fail("non-privileged request reached listener")

    monkeypatch.setattr(bot_modules.executors.urllib.request, "build_opener", must_not_call)
    logs = []
    router = make_router(bot_modules, logs)
    assert "not authorised" in router.dispatch(0, "/backup private argument")
    assert "private argument" not in "\n".join(logs)
    assert router.executors["/backup"].capability is bot_modules.router.Capability.PRIVILEGED
    assert "/backup" in router.dispatch(0, "/help")


async def test_library_backup_and_allowlisted_restart(
    telegram_app, bot_modules, backup_listener, monkeypatch
):
    app, request, _, _ = telegram_app
    await deliver(app, "/backup", user=1)
    assert request.calls[-1][1]["text"] == "Connect the SD card and send /backup again."
    assert backup_listener.calls == [("/backup", b"")]
    calls = []

    def restart(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bot_modules.executors.subprocess, "run", restart)
    await deliver(app, "/restart chrony", user=1)
    assert request.calls[-1][1]["text"] == "restarted chrony.service"
    assert calls[0][0] == ["/usr/bin/systemctl", "--no-ask-password", "restart", "chrony.service"]
    assert calls[0][1]["timeout"] == 30


def test_startup_empty_main_and_privileged_subset(bot_modules, tmp_path, monkeypatch):
    bot = bot_modules.bot
    allowlist = tmp_path / "allowlist"
    allowlist.write_text("# empty\n")
    monkeypatch.setenv("HOMELAB_BOT_ALLOWLIST", str(allowlist))
    with pytest.raises(SystemExit, match="empty"):
        bot.load_allowlist()
    allowlist.write_text("0 # owner\ninvalid\n")
    assert bot.load_allowlist() == {0}
    monkeypatch.setattr(bot, "load_token", lambda: "123:fixture")
    monkeypatch.setattr(bot, "load_privileged", lambda: {1})
    monkeypatch.setattr(bot, "load_restart_units", set)
    with pytest.raises(SystemExit, match="privileged allowlist but not"):
        bot.main()
    # main disables vendor logging; don't alter logging for unrelated tests.
    import logging

    logging.disable(logging.NOTSET)


def test_token_only_from_systemd_credential(bot_modules, monkeypatch, tmp_path):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.setenv("BOT_TOKEN", "must-not-use")
    with pytest.raises(SystemExit, match="CREDENTIALS_DIRECTORY"):
        bot_modules.bot.load_token()
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    (tmp_path / "bot-token").write_text("123:fixture\n")
    assert bot_modules.bot.load_token() == "123:fixture"


async def test_poll_outage_logs_once_and_recovery_without_content(bot_modules, monkeypatch):
    from telegram.error import NetworkError
    from telegram.ext import ExtBot

    logs = []
    monkeypatch.setattr(bot_modules.bot, "log", logs.append)
    results = iter([NetworkError("Private token and prose"), NetworkError("private"), (), ()])

    async def updates(*args, **kwargs):
        result = next(results)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ExtBot, "get_updates", updates)
    bot = bot_modules.bot.AuditedBot("123:fixture")
    for _ in range(2):
        with pytest.raises(NetworkError):
            await bot.get_updates()
    await bot.get_updates()
    await bot.get_updates()
    assert logs == [
        "WARNING: Telegram unreachable, backing off: NetworkError",
        "Telegram reachable again",
    ]
    await bot.shutdown()
