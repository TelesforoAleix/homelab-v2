import signal

import pytest

from tests.test_mac_profile import p, world  # noqa: F401


@pytest.fixture
def queue(world, monkeypatch):  # noqa: F811
    q = p.queue
    q.children.clear()
    clock = {"now": 1000, "ac": True}
    monkeypatch.setattr(p, "on_ac_power", lambda: clock["ac"])
    monkeypatch.setattr(p, "idle_seconds", lambda: 10000)
    monkeypatch.setattr(q.power.__globals__["time"], "time", lambda: clock["now"])
    monkeypatch.setattr(q, "memory", lambda pid=None: (20, 100))
    monkeypatch.setattr(q, "terminate", lambda job: q.children.pop(job.get("pid"), None))
    monkeypatch.setattr(p, "job_live", lambda j: bool(j.get("pid")))

    class Child:
        code = None

        def poll(self):
            return self.code

    def start(job, state):
        job.update(state="running", pid=42, started=clock["now"], attempt=job["attempt"] + 1)
        q.children[42] = Child()
        q.write(state)

    monkeypatch.setattr(q, "start", start)
    return q, clock


def add(q, name):
    return q.command("add", name, ["/usr/bin/true"])


def names(report, kind="waiting"):
    return [j["name"] for j in report[kind]]


def test_fifo_top_remove_and_logs(queue):
    q, _ = queue
    add(q, "one")
    add(q, "two")
    add(q, "three")
    assert names(q.command("top", "three")) == ["three", "one", "two"]
    assert names(q.command("remove", "one")) == ["three", "two"]
    assert not q.paths("one")[0].exists()
    assert q.paths("two")[0].stat().st_mode & 0o777 == 0o600
    q.command("play")
    q.step()
    assert q.running()["name"] == "three"
    q.children[42].code = 0
    q.step()
    assert q.running()["name"] == "two"
    assert names(q.report(), "finished") == ["three"]
    with pytest.raises(ValueError):
        q.command("remove", "two")


def test_stop_front_resume(queue):
    q, _ = queue
    add(q, "one")
    add(q, "two")
    q.command("play")
    q.step()
    q.command("top", "two")
    result = q.command("stop")
    assert not result["play"] and result["running"] is None
    assert names(result) == ["one", "two"]
    q.step()
    assert not q.running()
    q.command("play")
    q.step()
    assert q.running()["name"] == "one"
    assert q.running()["attempt"] == 1


def test_retry_failed_requeue_and_pass(queue):
    q, _ = queue
    add(q, "one")
    add(q, "two")
    q.command("play")
    q.step()
    q.children[42].code = 7
    q.step()
    assert q.running()["attempt"] == 2
    q.children[42].code = 8
    q.step()
    assert q.running()["name"] == "two"
    failed = q.report()["failed"][0]
    assert failed["exit_code"] == 8 and failed["attempt"] == 2
    assert names(q.command("requeue", "one")) == ["one"]
    assert q.read()["jobs"][-1]["attempt"] == 0
    q.children[42].code = 4
    q.step()
    q.children[42].code = 0
    q.step()
    assert names(q.report(), "finished") == ["two"]
    assert q.running()["name"] == "one"


def test_power_debounce_both_directions(queue):
    q, clock = queue
    add(q, "one")
    q.command("play")
    clock["ac"] = False
    q.step()
    assert not q.running()
    clock["ac"] = True
    q.step()
    assert q.running()
    clock["ac"] = False
    q.step()
    clock["now"] += 599
    q.step()
    assert q.running()
    clock["ac"] = True  # A brief reconnection resets continuous battery time.
    q.step()
    clock["ac"] = False
    q.step()
    clock["now"] += 600
    q.step()
    assert not q.running() and q.report()["paused_on_battery"] and q.read()["play"]
    clock["ac"] = True
    q.step()
    clock["now"] += 599
    q.step()
    assert not q.running()
    clock["ac"] = False
    q.step()
    clock["ac"] = True
    q.step()
    clock["now"] += 600
    q.step()
    assert q.running() and not q.report()["paused_on_battery"]


def test_report_power_wait_messages(queue):
    q, clock = queue
    add(q, "one")
    q.command("play")
    clock["ac"] = False
    q.step()
    report = q.report()
    assert report["power_source"] == "battery"
    assert report["power_wait_seconds"] == 600
    assert report["running"] is None and not report["paused_on_battery"]

    clock["ac"] = True
    q.step()
    q.step()
    assert q.running()
    clock["ac"] = False
    q.step()
    clock["now"] += 90
    report = q.report()
    assert report["power_source"] == "battery"
    assert report["power_wait_seconds"] == 510
    assert report["running"] is not None and not report["paused_on_battery"]

    clock["now"] += 510
    q.step()
    clock["ac"] = True
    q.step()
    clock["now"] += 60
    report = q.report()
    assert report["power_source"] == "ac"
    assert report["power_wait_seconds"] == 540
    assert report["running"] is None and report["paused_on_battery"]

    clock["now"] += 540
    q.step()
    report = q.report()
    assert report["power_wait_seconds"] is None
    assert not report["paused_on_battery"]


def test_scheduled_takes_front(queue, world, monkeypatch):  # noqa: F811
    q, _ = queue
    add(q, "one")
    add(q, "two")
    q.command("play")
    q.step()
    p.save(p.STATE / "jobs.json", {"scheduled": {"command": ["/usr/bin/true"]}})
    monkeypatch.setattr(p, "under_vscode", lambda: False)
    monkeypatch.setattr(p, "eligible", lambda cfg: True)
    p.job_worker("scheduled", world[0])
    assert p.jobs()["scheduled"]["pending"]
    q.children[42].code = 0
    q.step()
    assert not q.running()
    records = p.jobs()
    records["scheduled"].update(pending=False, pid=88)
    p.save(p.STATE / "jobs.json", records)
    q.step()
    assert not q.running()
    records["scheduled"]["pid"] = None
    p.save(p.STATE / "jobs.json", records)
    q.step()
    assert q.running()["name"] == "two"


def test_queue_counts_as_return_activity(queue, world):  # noqa: F811
    q, clock = queue
    cfg = world[0]
    p.switch("inference", cfg)
    add(q, "one")
    q.command("play")
    q.step()
    clock["now"] += 1000
    p.tick(cfg)
    assert p.load(p.STATE / "session.json", {})["last_active"] == clock["now"]
    assert p.snapshot(cfg)["return_pending"]


def test_report_progress(queue):
    q, clock = queue
    add(q, "one")
    q.command("play")
    q.step()
    q.paths("one")[1].write_text("1240/5000\nsecond line")
    clock["now"] += 12
    report = q.report()
    assert report["running"]["progress"] == "1240/5000"
    assert report["running"]["elapsed_seconds"] == 12
    assert report["running"]["physical_footprint_mb"] == 20
    assert report["free_memory_mb"] == 100


def test_terminate_whole_group_and_kill_deadline(queue, monkeypatch):
    q, _ = queue
    events = []
    timer = [0]
    monkeypatch.setattr(q, "signal_group", lambda pid, sig: events.append((pid, sig)))
    monkeypatch.setattr(q, "group_alive", lambda pid: not events or events[-1][1] != signal.SIGKILL)
    monkeypatch.setattr(q.power.__globals__["time"], "monotonic", lambda: timer[0])
    monkeypatch.setattr(
        q.power.__globals__["time"], "sleep", lambda s: timer.__setitem__(0, timer[0] + s)
    )
    monkeypatch.setattr(p, "router", lambda: {"identity": lambda pid: ""})
    type(q).terminate(q, {"pid": 42})
    assert events == [(42, signal.SIGTERM), (42, signal.SIGKILL)]
    assert 10 <= timer[0] < 10.1


def test_invalid_commands_and_launchd_guard(queue, monkeypatch):
    q, _ = queue
    for name, argv, cwd in [
        ("Bad", ["/bin/true"], None),
        ("valid", ["true"], None),
        ("valid", ["/bin/true"], "relative"),
    ]:
        with pytest.raises(ValueError):
            q.command("add", name, argv, cwd)
    add(q, "one")
    with pytest.raises(ValueError):
        add(q, "one")
    monkeypatch.setattr(p, "under_vscode", lambda: True)
    with pytest.raises(ValueError, match="launchd"):
        q.worker()


def test_requeue_stopped_to_end(queue):
    q, _ = queue
    add(q, "one")
    add(q, "two")
    q.command("play")
    q.step()
    q.command("stop")
    assert names(q.command("requeue", "one")) == ["two", "one"]


def test_retry_before_top_waiter(queue):
    q, _ = queue
    add(q, "one")
    add(q, "two")
    q.command("play")
    q.step()
    q.command("top", "two")
    q.children[42].code = 7
    q.step()
    assert q.running()["name"] == "one" and q.running()["attempt"] == 2


def test_spawn_records_pid_before_exec_ack_and_keeps_private_output(queue, monkeypatch):
    q, _ = queue
    add(q, "one")
    launches = []

    class Child:
        pid = 314

        def poll(self):
            return None

    def popen(argv, **kwargs):
        launches.append((argv, kwargs))
        if len(launches) == 1:
            assert q.running()["state"] == "running"
            assert "pid" not in q.running()
        else:
            assert q.running()["pid"] == 314
        return Child()

    monkeypatch.setattr(p, "router", lambda: {"identity": lambda pid: "birth"})
    monkeypatch.setattr(type(q).start.__globals__["subprocess"], "Popen", popen)
    state = q.read()
    type(q).start(q, state["jobs"][0], state)
    argv, kwargs = launches[0]
    assert argv[-1] == "/usr/bin/true"
    assert kwargs["start_new_session"] and "shell" not in kwargs
    assert kwargs["env"]["HOMELAB_PROFILE_JOB"] == "1"
    assert kwargs["env"]["HOMELAB_QUEUE_PROGRESS"] == str(q.paths("one")[1])
    assert q.running()["identity"] == "birth"
    assert launches[1][0] == ["/usr/bin/caffeinate", "-is", "-w", "314"]


def test_worker_restart_returns_orphan_to_front(queue):
    q, _ = queue
    add(q, "one")
    add(q, "two")
    q.command("play")
    q.step()
    q.command("top", "two")
    q.children.clear()
    q.step()
    assert not q.running()
    assert names(q.report()) == ["one", "two"]
    q.step()
    assert q.running()["name"] == "one"


def test_reused_pid_never_signalled(queue, monkeypatch):
    q, _ = queue
    monkeypatch.setattr(p, "router", lambda: {"identity": lambda pid: "new-owner"})
    monkeypatch.setattr(q, "signal_group", lambda *args: pytest.fail("signalled reused PID"))
    type(q).terminate(q, {"pid": 42, "identity": "old-owner"})


def test_memory_sums_only_job_group(queue, monkeypatch):
    q, _ = queue
    measured = []

    def footprint(pid):
        measured.append(pid)
        return {42: 20 * 1024**3, 43: 1024**3}[pid]

    def output(argv, **kwargs):
        assert kwargs["timeout"] == 5
        if argv[0] == "/bin/ps":
            assert argv[-1] == "pid=,pgid="
            return "42 42\n43 42\n99 99\n"
        assert argv == ["/usr/bin/memory_pressure", "-Q"]
        return (
            "The system has 51539607552 (3145728 pages with a page size of 16384).\n"
            "System-wide memory free percentage: 36%\n"
        )

    monkeypatch.setattr(p, "router", lambda: {"footprint": footprint})
    monkeypatch.setattr(type(q).memory.__globals__["subprocess"], "check_output", output)
    footprint_mb, available_mb = type(q).memory(q, 42)
    assert footprint_mb == 21 * 1024  # Charged Metal allocation, not RSS.
    assert available_mb == pytest.approx(48 * 1024 * 0.36)
    assert set(measured) == {42, 43}
    measured.clear()
    assert type(q).memory(q) == (None, available_mb)
    assert measured == []


@pytest.mark.parametrize("failure", ["ps", "footprint", "pressure", "malformed", "range", "empty"])
def test_memory_failures_are_unknown_and_independent(queue, monkeypatch, failure):
    q, _ = queue

    def footprint(pid):
        if failure == "footprint":
            raise ValueError("process exited")
        return 1024**2

    def output(argv, **kwargs):
        if argv[0] == "/bin/ps":
            if failure == "ps":
                raise OSError("unavailable")
            return "99 99\n" if failure == "empty" else "42 42\n"
        if failure == "pressure":
            raise OSError("unavailable")
        if failure == "malformed":
            return "unexpected output"
        percent = 101 if failure == "range" else 50
        return f"The system has 1073741824 (pages).\nSystem-wide memory free percentage: {percent}%"

    monkeypatch.setattr(p, "router", lambda: {"footprint": footprint})
    monkeypatch.setattr(type(q).memory.__globals__["subprocess"], "check_output", output)
    footprint_mb, available_mb = type(q).memory(q, 42)
    assert footprint_mb == (None if failure in ("ps", "footprint", "empty") else 1)
    assert available_mb == (None if failure in ("pressure", "malformed", "range") else 512)


def test_queue_cli_and_status_share_report(queue, monkeypatch, capsys):
    import json
    import sys

    q, _ = queue
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(p.SCRIPT),
            "queue",
            "add",
            "one",
            "--cwd",
            str(q.root),
            "--",
            "/usr/bin/true",
            "--flag",
        ],
    )
    assert p.main() == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["waiting"][0]["argv"] == ["/usr/bin/true", "--flag"]
    monkeypatch.setattr(sys, "argv", [str(p.SCRIPT), "status"])
    assert p.main() == 0
    assert json.loads(capsys.readouterr().out)["queue"] == q.report()


def test_exec_gate_requires_durable_parent_ack(tmp_path):
    import os
    import subprocess
    import sys

    helper = p.SCRIPT.with_name("profile_queue.py")
    marker = tmp_path / "ran"
    for acknowledged in (False, True):
        reader, writer = os.pipe()
        child = subprocess.Popen(
            [
                sys.executable,
                str(helper),
                str(reader),
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('ran')",
                str(marker),
            ],
            pass_fds=(reader,),
        )
        os.close(reader)
        if acknowledged:
            os.write(writer, b"1")
        os.close(writer)
        assert child.wait(timeout=5) == 0
        assert marker.exists() == acknowledged


def test_acceptance_dummy_journals_and_resumes(tmp_path):
    import ast
    import json
    import subprocess
    import sys

    source = p.SCRIPT.parents[1] / "tests/mac_queue_acceptance.py"
    tree = ast.parse(source.read_text())
    dummy = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "DUMMY" for target in node.targets)
    )
    script = tmp_path / "count.py"
    script.write_text(dummy)
    args = [sys.executable, str(script), str(tmp_path), "test", "2", "1", "no"]
    assert subprocess.run(args, check=False).returncode == 7
    subprocess.run(args, check=True)
    subprocess.run(args, check=True)
    journal = [json.loads(line) for line in (tmp_path / "events").read_text().splitlines()]
    assert [row[2] for row in journal if row[1] == "step"] == [1, 2]
    assert (tmp_path / "test.count").read_text() == "2"
