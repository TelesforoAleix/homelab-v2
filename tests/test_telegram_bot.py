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
    for name in (
        "router",
        "executors",
        "orders",
        "order_listener",
        "planning",
        "conversation",
        "bot",
    ):
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
        elif name in {"editMessageText", "answerCallbackQuery"}:
            result = True
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


@pytest.fixture
def planning_repo(bot_modules, tmp_path, monkeypatch):
    import subprocess

    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (source / "state.json").write_text(json.dumps({"version": 1, "slices": []}))
    (source / "outbox.jsonl").write_text("")

    def commit():
        git("add", ".")
        git("commit", "--allow-empty", "-m", "fixture")

    commit()
    logs = []
    planning = bot_modules.planning
    mirror = planning.Planning(
        planning.Config(str(source), "main", "state.json", "outbox.jsonl"),
        logs.append,
        directory=tmp_path / "data",
        ready=lambda: True,
    )
    environment = mirror.git_environment()
    environment["GIT_ALLOW_PROTOCOL"] = "file"
    monkeypatch.setattr(planning.Planning, "git_environment", lambda self: environment)
    return SimpleNamespace(mirror=mirror, source=source, commit=commit, logs=logs)


def planning_slice(**changes):
    return (
        dict(
            id="fixture/item",
            repo="fixture",
            feature="fixture",
            title="Fixture title",
            state="planned",
            slot=None,
            pr=None,
            attention=None,
        )
        | changes
    )


@pytest.mark.parametrize(
    "state,group",
    [
        ("planned", "planned"),
        ("researching", "planned"),
        ("ready", "ready"),
        ("issued", "running"),
        ("awaiting handover", "running"),
        ("in review", "needs attention"),
        ("escalated", "needs attention"),
    ],
)
def test_planning_every_state(bot_modules, state, group):
    groups = bot_modules.planning.group_pending([planning_slice(state=state)])
    assert [key for key, values in groups.items() if values] == [group]
    assert not any(bot_modules.planning.group_pending([planning_slice(state="accepted")]).values())


@pytest.mark.parametrize("kind", ["question", "escalation", "merge", "node-step", "owner-check"])
@pytest.mark.parametrize("resolved", [False, True])
def test_planning_attention_precedence(bot_modules, kind, resolved):
    attention = dict(
        kind=kind,
        body="Private body",
        since="2026-01-01T00:00:00Z",
        answer="Answer" if resolved else None,
        approved=resolved,
    )
    item = planning_slice(state="ready", slot="2026-01-01T00:00:00Z", attention=attention)
    groups = bot_modules.planning.group_pending([item])
    expected = (
        "needs attention" if not resolved or kind in ("node-step", "owner-check") else "scheduled"
    )
    assert groups[expected] == [item]
    item["state"] = "accepted"
    assert not any(bot_modules.planning.group_pending([item]).values())


def test_planning_commands_pull_and_group(planning_repo, bot_modules):
    fixture = planning_repo
    items = [
        planning_slice(id="fixture/later", state="ready", slot="2026-02-01T00:00:00Z"),
        planning_slice(id="fixture/earlier", slot="2026-01-01T00:00:00Z"),
        planning_slice(id="fixture/planned"),
    ]
    (fixture.source / "state.json").write_text(json.dumps({"version": 1, "slices": items}))
    fixture.commit()
    mirror = fixture.mirror
    result = mirror.command("pending", [])
    assert result.index("fixture/earlier") < result.index("fixture/later")
    assert "Planned: 1" in result
    assert "fixture/planned" not in result
    attention = dict(
        kind="question",
        body="Private body" * 1000,
        since="2026-01-01T00:00:00Z",
        answer=None,
        approved=False,
    )
    items[0]["attention"] = attention
    (fixture.source / "state.json").write_text(json.dumps({"version": 2, "slices": items}))
    fixture.commit()
    result = mirror.command("slice", ["fixture/later"])
    assert "id: fixture/later" in result
    assert "attention.body: " + attention["body"] in result
    assert "attention.answer: null" in result
    assert "attention.approved: false" in result
    assert mirror.command("slice", ["missing"]) == "Slice not found."
    assert "Private body" not in "\n".join(fixture.logs)
    assert "planning clone ready" in fixture.logs
    assert "planning pull succeeded" in fixture.logs
    router = make_router(bot_modules, fixture.logs)
    mirror.register(router)
    assert all(
        router.executors[name].capability is bot_modules.router.Capability.READ
        for name in ("/pending", "/recap", "/slice")
    )
    assert "/pending" in router.dispatch(0, "/help")
    router.dispatch(0, "/slice private-argument")
    assert "private-argument" not in "\n".join(fixture.logs)


def test_planning_locked_missing_and_invalid_config(planning_repo, bot_modules, tmp_path):
    mirror = planning_repo.mirror
    mirror.ready = lambda: False
    for name, args in [("pending", []), ("slice", ["fixture/item"]), ("recap", [])]:
        assert "locked" in mirror.command(name, args)
    assert not mirror.directory.exists()
    mirror.ready = lambda: True
    assert "Planned: 0" in mirror.command("pending", [])
    logs = []
    assert bot_modules.planning.load_config(logs.append, tmp_path / "absent") is None
    assert logs == ["planning not configured; feature disabled"]
    mirror.config = None
    assert mirror.command("pending", []) == "Planning is not configured."
    path = tmp_path / "bad"
    path.write_text("{private")
    assert bot_modules.planning.load_config(logs.append, path) is None
    assert "private" not in "\n".join(logs)


def test_planning_sync_failure_and_path_escape(planning_repo, monkeypatch):
    import subprocess

    mirror = planning_repo.mirror
    assert "Planned: 0" in mirror.command("pending", [])

    def fail(*args):
        raise subprocess.CalledProcessError(1, "Private URL", stderr="Private content")

    monkeypatch.setattr(mirror, "git", fail)
    assert "sync failed" in mirror.command("pending", [])
    assert "Private" not in "\n".join(planning_repo.logs)
    with pytest.raises(ValueError):
        mirror.read_file("../outside")
    (mirror.clone / "escape").symlink_to(planning_repo.source / "state.json")
    with pytest.raises(ValueError):
        mirror.read_file("escape")


def test_planning_recap_and_long_notice(planning_repo, bot_modules):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    events = [
        dict(
            at=(now - timedelta(hours=h)).isoformat(),
            id=f"fixture/{name}",
            event=event,
            text="Private text",
        )
        for h, name, event in [
            (25, "old", "start"),
            (2, "one", "start"),
            (1, "two", "attention"),
            (0, "one", "finished"),
        ]
    ]
    (planning_repo.source / "outbox.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events)
    )
    planning_repo.commit()
    reply = planning_repo.mirror.command("recap", [])
    assert "fixture/old" not in reply
    assert reply.count("fixture/one") == 1
    assert reply.index("finished") < reply.index("fixture/two")
    assert "fixture/old" in planning_repo.mirror.command("recap", ["48"])
    for args in (["nan"], ["inf"], ["0"], ["-1"], ["bad"], ["1", "2"]):
        assert planning_repo.mirror.command("recap", args).startswith("Usage:")
    text = bot_modules.planning.notice(events[0] | {"text": "X" * 3000})
    assert "X" * 600 in text and "X" * 601 not in text
    assert "/slice fixture/old" in text


def append_planning_event(fixture, **changes):
    from datetime import UTC, datetime

    event = (
        dict(
            at=datetime.now(UTC).isoformat(),
            id="fixture/item",
            event="attention",
            text="Private notice body",
        )
        | changes
    )
    with (fixture.source / "outbox.jsonl").open("a") as file:
        file.write(json.dumps(event) + "\n")
    fixture.commit()
    return event


async def test_planning_first_run_skips_history_and_once_across_restarts(
    planning_repo, bot_modules
):
    fixture = planning_repo
    append_planning_event(fixture, event="start")
    sent = []

    async def send(user, text):
        sent.append((user, text))

    await fixture.mirror.notify_once({0, 1}, send)
    assert sent == []
    assert json.loads(fixture.mirror.marker.read_text()) == {"line": 1, "sent_to": []}
    append_planning_event(fixture, event="finished", text="X" * 3000)
    await fixture.mirror.notify_once({0, 1}, send)
    assert [user for user, _ in sent] == [0, 1]
    assert all("X" * 600 in text and "X" * 601 not in text for _, text in sent)
    assert all("/slice fixture/item" in text for _, text in sent)
    assert json.loads(fixture.mirror.marker.read_text()) == {"line": 2, "sent_to": []}
    restarted = bot_modules.planning.Planning(
        fixture.mirror.config,
        fixture.logs.append,
        directory=fixture.mirror.directory,
        ready=lambda: True,
    )
    await restarted.notify_once({0, 1}, send)
    await restarted.notify_once({0, 1}, send)
    assert len(sent) == 2
    assert "Private notice body" not in "\n".join(fixture.logs)
    assert "X" * 20 not in "\n".join(fixture.logs)


async def test_planning_partial_delivery_persisted_immediately_and_retried(
    planning_repo, bot_modules
):
    fixture = planning_repo
    mirror = fixture.mirror
    sent = []

    async def send(user, text):
        sent.append(user)
        if user == 1:
            assert json.loads(mirror.marker.read_text()) == {"line": 0, "sent_to": [0]}
            raise OSError("Private failure")

    await mirror.notify_once({0, 1}, send)
    append_planning_event(fixture)
    with pytest.raises(OSError):
        await mirror.notify_once({0, 1}, send)
    assert sent == [0, 1]
    assert json.loads(mirror.marker.read_text()) == {"line": 0, "sent_to": [0]}
    restarted = bot_modules.planning.Planning(
        mirror.config, fixture.logs.append, directory=mirror.directory, ready=lambda: True
    )

    async def confirmed(user, text):
        sent.append(user)

    await restarted.notify_once({0, 1}, confirmed)
    assert sent == [0, 1, 1]
    assert json.loads(mirror.marker.read_text()) == {"line": 1, "sent_to": []}
    await restarted.notify_once({0, 1}, confirmed)
    assert sent == [0, 1, 1]


async def test_planning_marker_failure_retries_uncertain_delivery(planning_repo, monkeypatch):
    mirror = planning_repo.mirror
    sent = []

    async def send(user, text):
        sent.append(user)

    await mirror.notify_once({0}, send)
    append_planning_event(planning_repo)
    save = mirror.save_marker

    def fail(marker):
        raise OSError("Private path")

    monkeypatch.setattr(mirror, "save_marker", fail)
    with pytest.raises(OSError):
        await mirror.notify_once({0}, send)
    assert json.loads(mirror.marker.read_text()) == {"line": 0, "sent_to": []}
    monkeypatch.setattr(mirror, "save_marker", save)
    await mirror.notify_once({0}, send)
    assert sent == [0, 0]  # accepted duplicate window, never skip


async def test_planning_notices_wait_for_unlock(planning_repo):
    mirror = planning_repo.mirror
    sent = []

    async def send(user, text):
        sent.append(text)

    mirror.ready = lambda: False
    await mirror.notify_once({0}, send)
    assert not mirror.directory.exists()
    mirror.ready = lambda: True
    await mirror.notify_once({0}, send)
    append_planning_event(planning_repo)
    mirror.ready = lambda: False
    await mirror.notify_once({0}, send)
    assert sent == []
    assert json.loads(mirror.marker.read_text())["line"] == 0
    mirror.ready = lambda: True
    await mirror.notify_once({0}, send)
    assert len(sent) == 1


@pytest.mark.parametrize(
    "marker",
    [
        {"line": 99, "sent_to": []},
        {"line": -1, "sent_to": []},
        {"line": 0, "sent_to": [0]},
        {"line": False, "sent_to": []},
    ],
)
async def test_planning_invalid_marker_never_skips_or_replays(planning_repo, marker):
    mirror = planning_repo.mirror
    assert mirror.refresh() is None
    mirror.marker.write_text(json.dumps(marker))

    async def fail(*args):
        pytest.fail("invalid marker sent notices")

    with pytest.raises(ValueError):
        await mirror.notify_once({0}, fail)
    assert json.loads(mirror.marker.read_text()) == marker


async def test_planning_poll_failure_logs_no_content_and_retries(planning_repo, monkeypatch):
    import asyncio

    mirror = planning_repo.mirror
    attempts = []

    async def failure(*args):
        attempts.append(True)
        raise OSError("Private content and URL")

    async def stop(seconds):
        assert seconds == 60
        raise asyncio.CancelledError

    monkeypatch.setattr(mirror, "notify_once", failure)
    monkeypatch.setattr(asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await mirror.run({0}, failure)
    assert len(attempts) == 1
    assert planning_repo.logs == ["WARNING: planning notice failed; retry pending"]


async def test_planning_registered_and_gated_by_main_allowlist(telegram_app, planning_repo):
    app, request, router, logs = telegram_app
    planning_repo.mirror.register(router)
    await app.post_init(app)
    commands = dict(request.calls)["setMyCommands"]["commands"]
    assert {"pending", "recap", "slice"} <= {command["command"] for command in commands}
    assert len(dict(request.calls)["setMyDescription"]["description"]) <= 512
    await deliver(app, "/pending", user=99)
    assert request.calls[-1][1]["text"] == "Not authorised."
    assert not planning_repo.mirror.directory.exists()
    await deliver(app, "/pending", user=0)
    assert "Planned: 0" in request.calls[-1][1]["text"]
    await deliver(app, "/slice private-fixture-id", user=0)
    assert request.calls[-1][1]["text"] == "Slice not found."
    assert "private-fixture-id" not in "\n".join(logs)


async def test_planning_lifecycle_and_strict_send(bot_modules, monkeypatch, planning_repo):
    import asyncio

    from telegram.error import NetworkError
    from telegram.ext import ExtBot

    request = TelegramRequest()
    monkeypatch.setattr(
        bot_modules.bot,
        "AuditedBot",
        lambda token: ExtBot(token, request=request, get_updates_request=request),
    )
    mirror = planning_repo.mirror
    router = make_router(bot_modules, [])
    mirror.register(router)
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def worker(users, send):
        try:
            assert users == {0}
            await send(0, "Private notice")

            # Failures must reach Planning.run so the marker stays pending.
            async def failed(*args, **kwargs):
                raise OSError("Private error")

            monkeypatch.setattr(request, "do_request", failed)
            with pytest.raises(NetworkError):
                await send(0, "Retry me")
            entered.set()
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(mirror, "run", worker)
    app = bot_modules.bot.build_application("123:fixture", {0}, router, mirror)
    async with app:
        await app.post_init(app)
        await asyncio.wait_for(entered.wait(), timeout=5)
        await app.post_stop(app)
        assert cancelled.is_set()
        await app.post_shutdown(app)


async def test_planning_no_config_keeps_status_and_has_no_background_task(
    bot_modules, tmp_path, monkeypatch
):
    from telegram.ext import ExtBot

    logs = []
    planning = bot_modules.planning
    mirror = planning.Planning(
        planning.load_config(logs.append, tmp_path / "absent"), logs.append, ready=lambda: True
    )
    router = make_router(bot_modules, logs)
    mirror.register(router)
    request = TelegramRequest()
    monkeypatch.setattr(
        bot_modules.bot,
        "AuditedBot",
        lambda token: ExtBot(token, request=request, get_updates_request=request),
    )
    for name in ("host_name", "host_uptime", "host_load", "host_memory", "host_disk"):
        monkeypatch.setattr(bot_modules.executors, name, lambda: "fixture")

    async def must_not_run(*args):
        pytest.fail("disabled feature scheduled a background task")

    monkeypatch.setattr(mirror, "run", must_not_run)
    app = bot_modules.bot.build_application("123:fixture", {0}, router, mirror)
    async with app:
        await app.post_init(app)
        await deliver(app, "/status")
        assert "host:   fixture" in request.calls[-1][1]["text"]
        assert mirror.command("pending", []) == "Planning is not configured."
        await deliver(app, "/help")
        assert "/status" in request.calls[-1][1]["text"]
        await app.post_stop(app)
        await app.post_shutdown(app)
    assert logs.count("planning not configured; feature disabled") == 1


@pytest.mark.parametrize(
    "mounted,target_active,starts", [(True, False, True), (True, True, False), (False, False, True)]
)
def test_release_helper_waits_for_unmount_or_defers_to_unlock(
    tmp_path, mounted, target_active, starts
):
    import os
    import shlex
    import subprocess
    from configparser import ConfigParser

    unit = ConfigParser(interpolation=None)
    unit.read(BOT_DIR.parents[1] / "etc/systemd/system/homelab-bot-data-released.service")
    command = unit.get("Service", "ExecStart")
    mount_marker = tmp_path / "mounted"
    target_marker = tmp_path / "target-active"
    calls = tmp_path / "calls"
    if mounted:
        mount_marker.touch()
    if target_active:
        target_marker.touch()
    scripts = {
        "mountpoint": 'test -e "$FAKE_MOUNTED"',
        "systemctl": """case "$1" in
is-active) test -e "$FAKE_TARGET";;
start) printf '%s\\n' "$*" >> "$FAKE_CALLS";;
*) exit 1;;
esac""",
        "sleep": 'rm -f "$FAKE_MOUNTED"',
    }
    for name, script in scripts.items():
        path = tmp_path / name
        path.write_text("#!/bin/sh\n" + script + "\n")
        path.chmod(0o700)
        command = command.replace("/usr/bin/" + name, shlex.quote(str(path)))
    environment = dict(
        os.environ,
        FAKE_MOUNTED=str(mount_marker),
        FAKE_TARGET=str(target_marker),
        FAKE_CALLS=str(calls),
    )
    subprocess.run(
        shlex.split(command), env=environment, check=True, capture_output=True, timeout=5
    )
    assert calls.exists() is starts
    if starts:
        assert not mount_marker.exists()
        assert calls.read_text() == "start homelab-telegram-bot.service\n"


def test_planning_jsonl_final_newline_optional_and_invalid_line_blocks(planning_repo):
    event = append_planning_event(planning_repo, text="Fixture\u0085text")
    path = planning_repo.source / "outbox.jsonl"
    path.write_text(json.dumps(event, ensure_ascii=False))
    planning_repo.commit()
    mirror = planning_repo.mirror
    assert mirror.refresh() is None
    assert mirror.events() == [event]
    path.write_text(path.read_text() + "\n{")
    planning_repo.commit()
    assert mirror.refresh() is None
    with pytest.raises(ValueError):
        mirror.notification_batch()
    assert not mirror.marker.exists()


@pytest.fixture
def conversation_repo(planning_repo, bot_modules):
    from dataclasses import replace

    fixture = planning_repo
    (fixture.source / "overview.md").write_text("Private overview: fixture/item is ready.")
    (fixture.source / "backlog.md").write_text("Private backlog: fixture next feature.")
    (fixture.source / "detail.md").write_text("Private detail: more evidence.")
    (fixture.source / "binary.dat").write_bytes(b"\x00\xff")
    (fixture.source / "state.json").write_text(
        json.dumps({"slices": [planning_slice(state="ready")]})
    )
    fixture.commit()
    fixture.mirror.config = replace(
        fixture.mirror.config, context_files=["overview.md", "backlog.md"]
    )
    return fixture


def fake_frontline(bot_modules, fixture, replies, requests):
    import httpx

    def endpoint(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url) == "http://127.0.0.1:4000/v1/chat/completions"
        assert body["model"] == "frontline"
        assert body["max_tokens"] == bot_modules.conversation.REPLY_TOKENS
        response = replies.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, int):
            return httpx.Response(response)
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(response)}}]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    return bot_modules.conversation.Conversation(fixture.mirror, fixture.logs.append, client=client)


async def test_frontline_grounding_second_pass_and_content_free_logs(
    conversation_repo, bot_modules
):
    fixture = conversation_repo
    requests = []
    conversation = fake_frontline(
        bot_modules,
        fixture,
        [
            {"read_file": "detail.md", "answer": ""},
            {"read_file": None, "answer": "/restart ssh — Private grounded answer"},
        ],
        requests,
    )
    async with conversation.client:
        answer, key = await conversation.answer((10, 0), "Private fixture question")
    assert key is not None
    assert answer.startswith("/restart ssh")
    first = json.dumps(requests[0])
    assert "Private overview" in first and "Private backlog" in first
    assert "Fixture title" in first and "Ready (1)" in first
    assert "Private detail" not in first
    assert "Private detail" in json.dumps(requests[1])
    assert "Private" not in "\n".join(fixture.logs)
    assert not (fixture.mirror.clone / "new-file").exists()


@pytest.mark.parametrize(
    "name",
    ["../outside.md", "/etc/passwd", "binary.dat", ".git/config", "escape", "large.md", "fifo"],
)
def test_frontline_reader_rejects_unsafe_files(conversation_repo, bot_modules, tmp_path, name):
    import os

    fixture = conversation_repo
    assert fixture.mirror.refresh() is None
    clone = fixture.mirror.clone
    outside = tmp_path / "outside.md"
    outside.write_text("Private outside")
    (clone / "escape").symlink_to(outside)
    (clone / "large.md").write_bytes(b"a" * (bot_modules.conversation.FILE_LIMIT + 1))
    os.mkfifo(clone / "fifo")
    with pytest.raises((OSError, ValueError)):
        bot_modules.conversation.read_text(clone, name)


async def test_frontline_denied_read_second_pass_is_bounded(conversation_repo, bot_modules):
    requests = []
    conversation = fake_frontline(
        bot_modules,
        conversation_repo,
        [
            {"read_file": "../outside.md", "answer": ""},
            {"read_file": "detail.md", "answer": "no"},
        ],
        requests,
    )
    async with conversation.client:
        answer, key = await conversation.answer((10, 0), "Private question")
    assert answer == bot_modules.conversation.UNAVAILABLE and key is None
    assert len(requests) == 2
    assert "read_error" in json.dumps(requests[1])


async def test_frontline_episodes_new_topics_timeout_commands_and_reply_reopen(
    conversation_repo, bot_modules
):
    requests = []
    replies = [{"answer": "Private answer", "read_file": None} for _ in range(8)]
    conversation = fake_frontline(bot_modules, conversation_repo, replies, requests)
    now = [0]
    conversation.clock = lambda: now[0]
    owner = (10, 0)
    async with conversation.client:
        _, first = await conversation.answer(owner, "What is fixture about?")
        conversation.remember(owner, first, [100, 101])
        _, same = await conversation.answer(owner, "Why is that waiting?")
        assert same == first
        assert "What is fixture about?" in json.dumps(requests[-1])
        _, second = await conversation.answer(owner, "Explain another-project")
        assert second != first
        assert "What is fixture about?" not in json.dumps(requests[-1])
        _, reopened = await conversation.answer(owner, "More?", reply_to=101)
        assert reopened == first
        assert "Explain another-project" not in json.dumps(requests[-1])
        now[0] += 1801
        _, expired = await conversation.answer(owner, "Why is that?")
        assert expired != first
        assert "What is fixture about?" not in json.dumps(requests[-1])
        conversation.close(owner)
        _, closed = await conversation.answer(owner, "Why is that?")
        assert closed != expired
        _, other = await conversation.answer((10, 1), "More?", reply_to=100)
        assert other != first
        assert "What is fixture about?" not in json.dumps(requests[-1])
        _, reopened = await conversation.answer(owner, "More?", reply_to=100)
        assert reopened == first


@pytest.mark.parametrize("failure", ["locked", "down", "exception", "sync"])
async def test_frontline_unavailable_logs_no_content_and_commands_work(
    conversation_repo, bot_modules, monkeypatch, failure
):
    import httpx

    fixture = conversation_repo
    requests = []
    conversation = fake_frontline(
        bot_modules,
        fixture,
        [httpx.ConnectError("Private question") if failure == "exception" else 503],
        requests,
    )
    if failure == "locked":
        fixture.mirror.ready = lambda: False
    if failure == "sync":
        monkeypatch.setattr(fixture.mirror, "refresh", lambda: "Planning sync failed")
    async with conversation.client:
        answer, key = await conversation.answer((10, 0), "Private question")
    assert answer == bot_modules.conversation.UNAVAILABLE and key is None
    assert "Private" not in "\n".join(fixture.logs)
    router = make_router(bot_modules, fixture.logs)
    assert "/status" in router.dispatch(0, "/help")
    if failure == "locked":
        assert not fixture.mirror.directory.exists() and not requests


def test_paragraph_splitting_and_oversize_fallback(bot_modules):
    split = bot_modules.bot.split_message
    text = "a" * 3000 + "\n\n" + "b" * 2000 + "\n\n" + "c" * 2000
    parts = split(text)
    assert parts[0] == "a" * 3000 + "\n\n"
    assert "".join(parts) == text
    for text in ["😀" * 5000, "word " * 3000, ("A sentence. " * 1000), "x" * 4095 + "\n\nZ"]:
        parts = split(text)
        assert "".join(parts) == text
        assert all(len(p.encode("utf-16-le")) // 2 <= 4096 for p in parts)


@pytest.mark.parametrize("files", ["file.md", ["../escape"], ["/absolute"], [1], None])
def test_frontline_invalid_context_config(bot_modules, tmp_path, files):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            dict(
                repo_url="git@github.com:fixture/repo.git",
                branch="main",
                state_path="state.json",
                outbox_path="outbox.jsonl",
                context_files=files,
            )
        )
    )
    logs = []
    assert bot_modules.planning.load_config(logs.append, path) is None
    assert logs == ["WARNING: planning configuration invalid; feature disabled"]


async def test_frontline_telegram_gate_reply_mapping_and_commands(
    telegram_app, conversation_repo, bot_modules, monkeypatch
):
    app, request, router, logs = telegram_app
    seen = []
    conversation = fake_frontline(
        bot_modules,
        conversation_repo,
        [
            {"answer": "Private episode answer", "read_file": None},
            {"answer": "Different topic answer", "read_file": None},
            {"answer": "Reopened answer", "read_file": None},
        ],
        seen,
    )
    monkeypatch.setattr(bot_modules.bot, "Conversation", lambda *args: conversation)
    # Rebuild with the real receive closure and the already initialized fixture Bot.
    monkeypatch.setattr(bot_modules.bot, "AuditedBot", lambda token: app.bot)
    configured = bot_modules.bot.build_application(
        "123:fixture", {0, 1}, router, conversation_repo.mirror
    )
    async with conversation.client, configured:
        await deliver(configured, "Private unauthorized question", user=99)
        assert request.calls[-1][1]["text"] == "Not authorised."
        assert not seen and not conversation_repo.mirror.directory.exists()
        await deliver(configured, "Private question about fixture")
        await deliver(configured, "Explain another-project")
        assert "Private question about fixture" not in json.dumps(seen[-1])
        update = Update.de_json(
            {
                "update_id": 3,
                "message": {
                    "message_id": 3,
                    "date": 0,
                    "chat": {"id": 10, "type": "private"},
                    "from": {"id": 0, "is_bot": False, "first_name": "Fixture"},
                    "text": "More?",
                    "reply_to_message": {
                        "message_id": 1,
                        "date": 0,
                        "chat": {"id": 10, "type": "private"},
                        "from": {"id": 123, "is_bot": True, "first_name": "Fixture"},
                        "text": "Private episode answer",
                    },
                },
            },
            configured.bot,
        )
        # Fixture sendMessage normally reuses id 1. Map a distinct first answer id
        # so the reopened episode test reflects Telegram's unique message ids.
        first_key = next(iter(conversation.episodes))
        conversation.remember((10, 0), first_key, [1])
        await configured.process_update(update)
        assert "Private question about fixture" in json.dumps(seen[-1])
        assert "Explain another-project" not in json.dumps(seen[-1])
        await deliver(configured, "/help")
        assert (10, 0) not in conversation.active
        assert "Home Lab bot" in request.calls[-1][1]["text"]
    assert "Private" not in "\n".join(logs + conversation_repo.logs)


def test_long_slice_preserves_prose_paragraphs(conversation_repo, bot_modules):
    fixture = conversation_repo
    body = "First paragraph. " * 100 + "\n\n" + "Second paragraph. " * 150
    item = planning_slice(attention={"body": body, "kind": "question"})
    (fixture.source / "state.json").write_text(json.dumps({"slices": [item]}))
    fixture.commit()
    text = fixture.mirror.command("slice", [item["id"]])
    parts = bot_modules.bot.split_message(text)
    assert len(parts) > 1
    assert "\n\n" in text and body in text
    assert all(part.endswith("\n\n") for part in parts[:-1])
    assert "".join(parts) == text


@pytest.mark.parametrize(
    "first,next_question,follows",
    [
        ("What's next for med-ask?", "What about image-studio?", False),
        ("Qué sigue para med-ask?", "Qué sigue para image-studio?", False),
        ("What is FA3?", "Explain FA3 further", True),
        ("What is FA3?", "And FA4?", False),
    ],
)
def test_episode_topic_identifiers(bot_modules, first, next_question, follows):
    conversation = bot_modules.conversation
    episode = conversation.Episode(
        messages=[
            {"role": "user", "content": first},
            {"role": "assistant", "content": "Fixture answer"},
        ]
    )
    assert conversation.Conversation.follows(episode, next_question) is follows


@pytest.fixture
def order_repo(conversation_repo):
    fixture = conversation_repo
    item = planning_slice(state="ready", paused=False, stop_before_merge=False)
    (fixture.source / "state.json").write_text(json.dumps({"slices": [item]}))
    fixture.commit()
    fixture.item = item
    fixture.orders = fixture.mirror.orders
    return fixture


def write_order_state(fixture, **changes):
    fixture.item.update(changes)
    (fixture.source / "state.json").write_text(json.dumps({"slices": [fixture.item]}))
    fixture.commit()


def proposal(action="start", argument=None, target="fixture/item"):
    return {"action": action, "id": target, "argument": argument}


def attention(kind="question", **changes):
    return (
        dict(
            kind=kind,
            body="Private attention",
            since="2026-10-20T10:00:00Z",
            answer=None,
            approved=False,
        )
        | changes
    )


@pytest.mark.parametrize(
    "action,state,att,argument,allowed",
    [
        ("start", "ready", None, None, True),
        ("start", "issued", None, None, False),
        ("slot", "ready", None, "tomorrow at 10", True),
        ("slot", "planned", None, "tomorrow at 10", False),
        ("pause", "issued", None, None, True),
        ("pause", "accepted", None, None, False),
        ("resume", "ready", None, None, True),
        ("resume", "accepted", None, None, False),
        ("answer", "escalated", attention(), "Private owner words", True),
        ("answer", "escalated", attention("escalation"), "Private owner words", True),
        ("answer", "escalated", attention(answer="answered"), "Private owner words", False),
        ("answer", "ready", None, "Private owner words", False),
        ("approve", "in review", attention("merge"), None, False),
        (
            "approve",
            "in review",
            attention("merge", review={"verdict": "Private verdict"}),
            None,
            True,
        ),
        (
            "approve",
            "in review",
            attention("merge", approved=True, review={"verdict": "Private verdict"}),
            None,
            False,
        ),
        ("approve", "ready", attention(review={"verdict": "Private verdict"}), None, False),
    ],
)
@pytest.mark.parametrize("stop_before_merge", [False, True])
def test_order_preconditions(order_repo, action, state, att, argument, allowed, stop_before_merge):
    fixture = order_repo
    write_order_state(fixture, state=state, attention=att, stop_before_merge=stop_before_merge)
    if allowed:
        order = fixture.orders.propose(proposal(action, argument), "Private owner words", (10, 0))
        assert order["status"] == "proposed" and fixture.orders.queued() == []
        assert order["expect"] == {
            "state": state,
            "paused": False,
            "attention": {"kind": att["kind"], "since": att["since"]} if att else None,
        }
        if action == "approve":
            assert "Private verdict" in order["confirmation"]
    else:
        with pytest.raises(ValueError):
            fixture.orders.propose(proposal(action, argument), "Private owner words", (10, 0))
    assert "Private" not in "\n".join(fixture.logs)


@pytest.mark.parametrize(
    "target,allowed",
    [("fixture", True), ("development", True), ("unknown", False), ("fixture/item", False)],
)
def test_orchestrator_proposal(order_repo, target, allowed):
    fixture = order_repo
    if not allowed:
        with pytest.raises(ValueError):
            fixture.orders.propose(proposal("orchestrator", "Private task", target), "", (10, 0))
        return
    order = fixture.orders.propose(proposal("orchestrator", "Private task", target), "", (10, 0))
    assert order["expect"] is None
    assert fixture.orders.queued() == []
    fixture.orders.tap(order["id"], True, 10)
    write_order_state(fixture, state="accepted")
    with fixture.mirror.lock:
        assert fixture.mirror.refresh() is None
    assert fixture.orders.queued()[0]["expect"] is None


def test_order_verbatim_answer_and_pr_link(order_repo, bot_modules):
    from dataclasses import replace

    fixture = order_repo
    words = "Sí.\n  Use the original — exactly!  "
    write_order_state(fixture, attention=attention())
    with pytest.raises(ValueError):
        fixture.orders.propose(proposal("answer", "Use a paraphrase"), words, (10, 0))
    order = fixture.orders.propose(proposal("answer", words), "Answer: " + words, (10, 0))
    assert order["argument"] == words and words in order["confirmation"]
    fixture.mirror.config = replace(fixture.mirror.config, github_owner="fixture-owner")
    write_order_state(
        fixture,
        pr=42,
        attention=attention(
            "merge",
            review={
                "verdict": "Private verdict\nApproved with caveats.",
                "at": "now",
                "actor": "fixture",
            },
        ),
    )
    order = fixture.orders.propose(proposal("approve"), "approve", (10, 0))
    assert "PR #42\nhttps://github.com/fixture-owner/fixture/pull/42" in order["confirmation"]
    assert "Private verdict\nApproved with caveats." in order["confirmation"]
    assert "Private" not in "\n".join(fixture.logs)
    config = fixture.mirror.directory / "config.json"
    config.write_text(
        json.dumps(
            {
                "repo_url": "git@github.com:fixture/planning.git",
                "branch": "main",
                "state_path": "state.json",
                "outbox_path": "outbox.jsonl",
                "github_owner": "bad/owner",
            }
        )
    )
    assert bot_modules.planning.load_config(fixture.logs.append, config) is None


@pytest.mark.parametrize("yes,status", [(True, "queued"), (False, "cancelled")])
def test_order_taps_restart_and_metadata(order_repo, bot_modules, yes, status):
    fixture = order_repo
    order = fixture.orders.propose(proposal(), "start", (10, 0))
    assert fixture.orders.tap(order["id"], yes, 999) == ("Unknown order.", None)
    assert fixture.orders.tap(order["id"], yes, 10) == (status, [10, 0])
    assert fixture.orders.tap(order["id"], not yes, 10) == ("already " + status, [10, 0])
    restarted = bot_modules.orders.Orders(fixture.mirror)
    assert len(restarted.queued()) == int(yes)
    assert restarted.path.stat().st_mode & 0o777 == 0o600
    if yes:
        assert set(restarted.queued()[0]) == set(bot_modules.orders.PUBLIC)
    fixture.mirror.ready = lambda: False
    assert "unlocked" in fixture.orders.tap(order["id"], yes, 10)[0]
    with pytest.raises(ValueError):
        fixture.orders.queued()


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize(
    "change,reason",
    [
        ({"state": "issued"}, "state changed from ready to issued"),
        ({"paused": True}, "pause state changed"),
        ({"attention": attention()}, "attention changed"),
    ],
)
def test_order_overtaken_every_pull(order_repo, queued, change, reason):
    fixture = order_repo
    order = fixture.orders.propose(proposal(), "start", (10, 0))
    if queued:
        fixture.orders.tap(order["id"], True, 10)
    write_order_state(fixture, **change)
    # A normal command pull must reconcile orders too.
    fixture.mirror.command("pending", [])
    current = fixture.orders.load()[order["id"]]
    assert current["status"] == "overtaken" and current["detail"] == reason
    assert fixture.orders.queued() == []
    assert fixture.orders.tap(order["id"], True, 10)[0] == "already overtaken"
    assert fixture.orders.ack(order["id"], {"outcome": "done", "detail": "Private"}) == 204
    assert fixture.orders.load()[order["id"]]["status"] == "overtaken"


def test_answer_overtaken_same_attention_timestamp(order_repo):
    fixture = order_repo
    write_order_state(fixture, attention=attention())
    order = fixture.orders.propose(proposal("answer", "Private words"), "Private words", (10, 0))
    write_order_state(fixture, attention=attention(answer="Private orchestrator answer"))
    # Tap pulls first and must not enqueue even though expect itself still matches.
    assert fixture.orders.tap(order["id"], True, 10)[0] == "already overtaken"
    assert fixture.orders.load()[order["id"]]["detail"] == "answered from the orchestrator"


async def test_model_proposal_is_read_only_and_rejects_paraphrase(order_repo, bot_modules):
    fixture = order_repo
    requests = []
    conversation = fake_frontline(
        bot_modules,
        fixture,
        [
            {"answer": "Private proposal", "read_file": None, "order": proposal()},
        ],
        requests,
    )
    async with conversation.client:
        _, key = await conversation.answer((10, 0), "Start fixture/item")
    assert key is not None
    assert conversation.proposals[(10, 0)] == proposal()
    assert not fixture.orders.path.exists()  # Model output alone cannot even create a proposal.
    assert "only an allowlisted owner's Yes queues" in requests[0]["messages"][0]["content"]
    assert "paused" in json.dumps(requests[0])
    write_order_state(fixture, attention=attention())
    conversation = fake_frontline(
        bot_modules,
        fixture,
        [
            {"answer": "Private", "read_file": None, "order": proposal("answer", "Paraphrase")},
        ],
        [],
    )
    async with conversation.client:
        answer, key = await conversation.answer((10, 0), "Owner's original words")
    assert answer == bot_modules.conversation.UNAVAILABLE and key is None
    assert not conversation.proposals
    assert "Private" not in "\n".join(fixture.logs)


@pytest.fixture
async def order_app(order_repo, bot_modules, monkeypatch):
    from telegram.ext import ExtBot

    request = TelegramRequest()
    monkeypatch.setattr(
        bot_modules.bot,
        "AuditedBot",
        lambda token: ExtBot(
            token,
            request=request,
            get_updates_request=request,
        ),
    )
    conversation = fake_frontline(
        bot_modules,
        order_repo,
        [
            {"answer": "Private proposal", "read_file": None, "order": proposal()},
        ],
        [],
    )
    monkeypatch.setattr(bot_modules.bot, "Conversation", lambda *args: conversation)
    logs = order_repo.logs
    monkeypatch.setattr(bot_modules.bot, "log", logs.append)
    app = bot_modules.bot.build_application(
        "123:fixture",
        {0, 1},
        make_router(bot_modules, logs),
        order_repo.mirror,
    )
    async with conversation.client, app:
        yield SimpleNamespace(
            app=app, request=request, conversation=conversation, fixture=order_repo
        )


async def tap(app, order_id, choice="yes", user=0, chat=10):
    update = Update.de_json(
        {
            "update_id": 2,
            "callback_query": {
                "id": "fixture-callback",
                "chat_instance": "fixture",
                "from": {"id": user, "is_bot": False, "first_name": "Fixture"},
                "data": f"order:{choice}:{order_id}",
                "message": {"message_id": 1, "date": 0, "chat": {"id": chat, "type": "private"}},
            },
        },
        app.bot,
    )
    await app.process_update(update)


@pytest.mark.parametrize("choice,status", [("yes", "queued"), ("no", "cancelled")])
async def test_confirmation_callbacks_and_episode_close(order_app, choice, status):
    fixture, app, request = order_app.fixture, order_app.app, order_app.request
    await deliver(app, "Start fixture/item", user=999)
    assert not fixture.orders.path.exists()
    await deliver(app, "Start fixture/item")
    order = next(iter(fixture.orders.load().values()))
    assert fixture.orders.queued() == []
    sent = [params for name, params in request.calls if name == "sendMessage"]
    assert sent[-1]["text"] == "Queue start fixture/item?"
    buttons = sent[-1]["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["Yes", "No"]
    assert (10, 0) in order_app.conversation.active
    await tap(app, order["id"], choice, user=999)
    assert request.calls[-1][1]["text"] == "Not authorised."
    assert fixture.orders.queued() == []
    await tap(app, order["id"], choice)
    assert (10, 0) not in order_app.conversation.active
    edits = [params for name, params in request.calls if name == "editMessageText"]
    assert edits[-1]["text"] == status.capitalize()
    assert not edits[-1].get("reply_markup")
    await tap(app, order["id"], choice)
    assert request.calls[-1][1]["text"] == "already " + status
    assert fixture.orders.load()[order["id"]]["status"] == status
    assert "Private" not in "\n".join(fixture.logs)


async def test_ack_edits_and_retries_after_telegram_failure(order_app, monkeypatch):
    import asyncio

    fixture, app, request = order_app.fixture, order_app.app, order_app.request
    await deliver(app, "Start fixture/item")
    order = next(iter(fixture.orders.load().values()))
    await tap(app, order["id"])
    fixture.orders.ack(order["id"], {"outcome": "dropped", "detail": "Private runner detail"})
    original = request.do_request

    async def fail_edit(url, *args, **kwargs):
        if url.endswith("editMessageText"):
            raise OSError("Private network failure")
        return await original(url, *args, **kwargs)

    monkeypatch.setattr(request, "do_request", fail_edit)
    # A late tap also attempts outstanding edits.
    await tap(app, order["id"])
    assert fixture.orders.pending_edits()
    monkeypatch.setattr(request, "do_request", original)
    await tap(app, order["id"])
    assert not fixture.orders.pending_edits()
    assert [p for n, p in request.calls if n == "editMessageText"][-1]["text"] == (
        "Dropped: Private runner detail"
    )
    assert "Private" not in "\n".join(fixture.logs)

    # Lifecycle starts retry workers and cancels them without contacting a real interface.
    async def no_listener(self):
        await asyncio.Future()

    import order_listener

    monkeypatch.setattr(order_listener.Listener, "run", no_listener)
    await app.post_init(app)
    await asyncio.sleep(0)
    await app.post_stop(app)


@pytest.fixture
def order_http(order_repo, bot_modules):
    import threading

    listener = bot_modules.order_listener
    # Only the test server uses loopback and an ephemeral port. Production bind is tested below.
    server = listener.Server(("127.0.0.1", 0), listener.handler_for(order_repo.orders))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield order_repo, server.server_address
    server.shutdown()
    server.server_close()
    thread.join()


def http_order(address, path="/orders", method="GET", body=None, headers=None):
    import http.client

    connection = http.client.HTTPConnection(*address, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


def test_order_http_contract(order_http, bot_modules):
    fixture, address = order_http
    assert http_order(address) == (200, b'{"orders": []}')
    first = fixture.orders.propose(proposal(), "start", (10, 0))
    second = fixture.orders.propose(proposal("pause"), "pause", (10, 0))
    assert http_order(address) == (200, b'{"orders": []}')
    fixture.orders.tap(second["id"], True, 10)
    fixture.orders.tap(first["id"], True, 10)
    expected = fixture.orders.queued()
    assert [o["id"] for o in expected] == [first["id"], second["id"]]
    assert json.loads(http_order(address)[1]) == {"orders": expected}
    assert json.loads(http_order(address)[1]) == {"orders": expected}
    path = f"/orders/{first['id']}/ack"
    for outcome in ["dropped", "done", "failed"]:
        assert http_order(
            address,
            path,
            "POST",
            json.dumps(
                {
                    "outcome": outcome,
                    "detail": "Private runner detail",
                }
            ),
        ) == (204, b"")
    assert fixture.orders.load()[first["id"]]["status"] == "dropped"
    assert len(json.loads(http_order(address)[1])["orders"]) == 1
    assert fixture.orders.load()[first["id"]]["dirty"]
    assert (
        http_order(
            address,
            f"/orders/{'0' * 32}/ack",
            "POST",
            json.dumps(
                {
                    "outcome": "done",
                    "detail": "",
                }
            ),
        )[0]
        == 404
    )
    for body in [
        "no JSON",
        "[]",
        "null",
        "{}",
        '{"outcome":"other","detail":""}',
        '{"outcome":"done","detail":4}',
        '{"outcome":"done"}',
    ]:
        assert http_order(address, path, "POST", body)[0] == 400
    assert http_order(address, path, "POST", "a" * 4097)[0] == 413
    assert http_order(address, path, "POST", "{}", {"Transfer-Encoding": "chunked"})[0] == 400
    for method, route in [
        ("GET", "/"),
        ("POST", "/orders"),
        ("GET", path),
        ("GET", "/orders?x=1"),
        ("PUT", "/orders"),
        ("OPTIONS", "/orders"),
        ("TRACE", "/orders"),
        ("GET", "/orders/"),
        ("POST", path + "/"),
    ]:
        assert http_order(address, route, method)[0] == 404
    fixture.mirror.ready = lambda: False
    assert http_order(address)[0] == 503
    assert http_order(address, path, "POST", "{}")[0] == 503
    assert http_order(address, "/unknown")[0] == 404
    assert "Private" not in "\n".join(fixture.logs)


@pytest.mark.parametrize(
    "outcome,text",
    [
        ("done", "Done"),
        ("failed", "Failed: Private detail"),
        ("dropped", "Dropped: Private detail"),
    ],
)
def test_order_ack_statuses(order_repo, bot_modules, outcome, text):
    fixture = order_repo
    order = fixture.orders.propose(proposal(), "start", (10, 0))
    fixture.orders.tap(order["id"], True, 10)
    fixture.orders.ack(order["id"], {"outcome": outcome, "detail": "Private detail"})
    assert fixture.orders.queued() == []
    assert bot_modules.orders.status_text(fixture.orders.load()[order["id"]]) == text


async def test_listener_retries_only_tailscale(order_repo, bot_modules, monkeypatch):
    import asyncio

    module = bot_modules.order_listener
    calls, sleeps = [], []
    results = [OSError("no interface"), "100.64.1.2"]

    def address():
        value = results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    class FakeServer:
        def __init__(self, bind, handler):
            calls.append(bind)

        def serve_forever(self):
            pass

        def shutdown(self):
            pass

        def server_close(self):
            pass

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(module, "tailscale_address", address)
    monkeypatch.setattr(module, "Server", FakeServer)
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    listener = module.Listener(order_repo.orders, order_repo.logs.append)
    await listener.run()
    assert sleeps == [5] and calls == [("100.64.1.2", 8110)]
    await listener.stop()
    assert listener.server is None
    await asyncio.to_thread(lambda: None)


@pytest.mark.parametrize(
    "output,valid",
    [
        ("100.64.1.2\n", True),
        ("", False),
        ("127.0.0.1", False),
        ("0.0.0.0", False),
        ("192.168.1.2", False),
        ("::1", False),
    ],
)
def test_listener_runtime_address_validation(bot_modules, monkeypatch, output, valid):
    module = bot_modules.order_listener
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=output)

    monkeypatch.setattr(module.subprocess, "run", run)
    if valid:
        assert module.tailscale_address() == output.strip()
    else:
        with pytest.raises(ValueError):
            module.tailscale_address()
    assert calls == [["/usr/bin/tailscale", "ip", "-4"]]


async def test_long_answer_confirmation_verbatim_before_buttons(order_app, bot_modules):
    fixture = order_app.fixture
    words = "Private owner words — 😀\n" * 220
    write_order_state(fixture, attention=attention())

    async def completion(messages):
        return {"answer": "", "read_file": None, "order": proposal("answer", words)}

    order_app.conversation.completion = completion
    await deliver(order_app.app, "Answer with: " + words)
    sent = [params for name, params in order_app.request.calls if name == "sendMessage"]
    expected = "Queue answer fixture/item?\n\n" + words
    assert "".join(part["text"] for part in sent[:-1]) == expected
    assert all(not part.get("reply_markup") for part in sent[:-1])
    assert sent[-1]["text"] == "Queue answer fixture/item as shown above?"
    assert sent[-1]["reply_markup"]
    assert all(len(part["text"].encode("utf-16-le")) // 2 <= 4096 for part in sent)
    assert fixture.orders.queued() == []
    order = next(iter(fixture.orders.load().values()))
    await tap(order_app.app, order["id"])
    assert fixture.orders.queued()[0]["argument"] == words


async def test_overtaken_message_edit_survives_restart(order_app, bot_modules):
    fixture = order_app.fixture
    await deliver(order_app.app, "Start fixture/item")
    order = next(iter(fixture.orders.load().values()))
    write_order_state(fixture, paused=True)
    fixture.mirror.command("pending", [])
    restarted = bot_modules.orders.Orders(fixture.mirror)
    assert restarted.pending_edits()[0]["status"] == "overtaken"
    await tap(order_app.app, order["id"])
    replies = [p["text"] for n, p in order_app.request.calls if n == "answerCallbackQuery"]
    assert replies[-1] == "already overtaken"
    edits = [p for n, p in order_app.request.calls if n == "editMessageText"]
    assert edits[-1]["text"] == "Overtaken: pause state changed"
    assert not edits[-1].get("reply_markup")
    assert not restarted.pending_edits()


def test_order_invalid_proposals_and_atomic_write_failure(order_repo, bot_modules, monkeypatch):
    fixture = order_repo
    for invalid in [
        [],
        [proposal(), proposal()],
        proposal("restart"),
        proposal(target="unknown"),
        proposal("start", "unexpected"),
        proposal("slot"),
        proposal("answer", ""),
        {"action": "start", "id": "fixture/item"},
    ]:
        with pytest.raises(ValueError):
            fixture.orders.propose(invalid, "Private words", (10, 0))
    order = fixture.orders.propose(proposal(), "start", (10, 0))
    original = fixture.orders.path.read_bytes()

    def fail_replace(*args):
        raise OSError("Private disk failure")

    monkeypatch.setattr(bot_modules.orders.os, "replace", fail_replace)
    with pytest.raises(OSError):
        fixture.orders.tap(order["id"], True, 10)
    assert fixture.orders.path.read_bytes() == original
    assert fixture.orders.queued() == []
    assert list(fixture.mirror.directory.glob("tmp*")) == []
    assert "Private" not in "\n".join(fixture.logs)


def test_order_http_body_framing_and_no_content_logging(order_http, capsys):
    import socket

    fixture, address = order_http
    order = fixture.orders.propose(proposal(), "start", (10, 0))
    path = f"/orders/{order['id']}/ack"
    requests = [
        f"POST {path} HTTP/1.0\r\n\r\n",
        f"POST {path} HTTP/1.0\r\nContent-Length: -1\r\n\r\n",
        f"POST {path} HTTP/1.0\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{{}}",
        f"POST {path} HTTP/1.0\r\nContent-Length: 50\r\n\r\nPrivate partial body",
    ]
    for request in requests:
        with socket.create_connection(address, timeout=5) as connection:
            connection.sendall(request.encode())
            connection.shutdown(socket.SHUT_WR)
            assert connection.recv(1024).startswith(b"HTTP/1.0 400")
    assert http_order(address, path, "POST", "Private invalid JSON")[0] == 400
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("yes,status", [(False, "cancelled"), (True, "proposed")])
def test_order_no_cancels_without_network_but_yes_requires_pull(
    order_repo, monkeypatch, yes, status
):
    fixture = order_repo
    order = fixture.orders.propose(proposal(), "start", (10, 0))
    pulls = []

    def failed_refresh():
        pulls.append(True)
        return "Planning sync failed."

    monkeypatch.setattr(fixture.mirror, "refresh", failed_refresh)
    reply, owner = fixture.orders.tap(order["id"], yes, 10)
    assert bool(pulls) is yes
    assert fixture.orders.load()[order["id"]]["status"] == status
    assert fixture.orders.queued() == []
    if yes:
        assert "sync failed" in reply and owner is None
    else:
        assert reply == "cancelled" and owner == [10, 0]
