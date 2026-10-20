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
    for name in ("router", "executors", "planning", "conversation", "bot"):
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
