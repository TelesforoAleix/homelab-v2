"""Orchestrator-only proof on the installed Mac copy, with disposable Python jobs.

Run on AC, in a profile permitting scheduled jobs, and leave input idle for that
profile's idle interval. Refuses an active/playing/waiting queue. Never changes
profiles, services, the router, or ports. Only its own queue/calendar entries and
scratch files are removed. Run: uv run python tests/mac_queue_acceptance.py
"""

import importlib.machinery
import importlib.util
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

INSTALLED = Path.home() / "Library/Application Support/homelab/mac"
loader = importlib.machinery.SourceFileLoader(
    "queue_acceptance_profile", str(INSTALLED / "homelab-profile")
)
spec = importlib.util.spec_from_loader(loader.name, loader)
p = importlib.util.module_from_spec(spec)
loader.exec_module(p)
PYTHON = json.loads((INSTALLED / "install.json").read_text())["python"]

DUMMY = """import json, os, signal, subprocess, sys, time
from pathlib import Path
root, name, total, failures, descendant = sys.argv[1:]
root = Path(root)
count = root / (name + ".count")
attempts = root / (name + ".attempts")
attempt = int(attempts.read_text()) + 1 if attempts.exists() else 1
attempts.write_text(str(attempt))
def event(kind, value=attempt):
    with (root / "events").open("a") as f:
        f.write(json.dumps([name, kind, value]) + "\\n")
        f.flush()
        os.fsync(f.fileno())
event("start")
if descendant == "yes":
    child = subprocess.Popen([sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(3600)"])
    (root / (name + ".child")).write_text(str(child.pid))
    time.sleep(.2)
if attempt <= int(failures):
    event("fail")
    sys.exit(7)
for i in range(int(count.read_text()) if count.exists() else 0, int(total)):
    time.sleep(.25)
    temporary = count.with_suffix(".tmp")
    temporary.write_text(str(i + 1))
    temporary.replace(count)
    event("step", i + 1)
    progress = os.environ.get("HOMELAB_QUEUE_PROGRESS", str(root / "scheduled.progress"))
    Path(progress).write_text(str(i + 1))
event("end")
"""


def cli(*args):
    result = subprocess.run(
        [PYTHON, str(INSTALLED / "homelab-profile"), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def wait_for(predicate, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.2)
    raise AssertionError("acceptance condition timed out")


def main():
    initial = cli("queue", "list")
    assert not initial["play"] and not initial["running"] and not initial["waiting"], (
        "stop and drain the queue before acceptance"
    )
    assert p.on_ac_power(), "AC power required"
    cfg = p.config()
    status = cli("status")
    assert status["profile"] in cfg["profiles"] and cfg["profiles"][status["profile"]]["jobs"], (
        "scheduled test needs a permitting profile"
    )
    assert not any(j.get("pending") or p.job_live(j) for j in p.jobs().values()), (
        "wait for existing scheduled work to finish"
    )
    prefix = "jq-proof-" + uuid.uuid4().hex[:8]
    added = []
    scheduled = None
    with tempfile.TemporaryDirectory(prefix=prefix, dir=p.STATE) as folder:
        root = Path(folder)
        script = root / "count.py"
        script.write_text(DUMMY)

        def add(suffix, count=2, failures=0, descendant=False):
            name = prefix + "-" + suffix
            cli(
                "queue",
                "add",
                name,
                "--cwd",
                str(root),
                "--",
                PYTHON,
                str(script),
                str(root),
                suffix,
                str(count),
                str(failures),
                "yes" if descendant else "no",
            )
            added.append(name)
            return name

        def finished(name):
            return any(j["name"] == name for j in cli("queue", "list")["finished"])

        def events():
            return [json.loads(line) for line in (root / "events").read_text().splitlines()]

        try:
            first = add("first")
            second = add("second")
            third = add("third")
            cli("queue", "top", third)
            cli("queue", "play")
            wait_for(lambda: finished(second))
            assert [e[0] for e in events() if e[1] == "start"] == ["third", "first", "second"]
            assert finished(first)
            cli("queue", "stop")
            print("PASS FIFO, top, play")

            resumable = add("resume", count=40, descendant=True)
            cli("queue", "play")
            wait_for(lambda: (root / "resume.count").exists())
            child_pid = int((root / "resume.child").read_text())
            before = int((root / "resume.count").read_text())
            started = time.monotonic()
            stopped = cli("queue", "stop")
            assert time.monotonic() - started < 12, "group stop exceeded 10s plus CLI overhead"
            assert stopped["waiting"][0]["name"] == resumable
            # Read-only process observation; never signal a PID from this proof.
            observed = subprocess.run(
                ["/bin/ps", "-p", str(child_pid), "-o", "stat="], capture_output=True, text=True
            )
            assert observed.returncode != 0 or observed.stdout.strip().startswith("Z")
            after = int((root / "resume.count").read_text())
            assert after >= before
            cli("queue", "play")
            wait_for(lambda: finished(resumable))
            assert int((root / "resume.count").read_text()) == 40
            assert len([e for e in events() if e[:2] == ["resume", "start"]]) == 2
            cli("queue", "stop")
            print("PASS whole-group stop and journal resume")

            fails = add("fails", failures=2)
            passes = add("passes", failures=1)
            cli("queue", "play")
            wait_for(lambda: finished(passes))
            report = cli("queue", "list")
            failed = next(j for j in report["failed"] if j["name"] == fails)
            assert failed["attempt"] == 2 and failed["exit_code"] == 7
            assert next(j for j in report["finished"] if j["name"] == passes)["attempt"] == 2
            cli("queue", "stop")
            print("PASS retry once, failed mark, retry then pass")

            blocker = add("blocker", count=16)
            tail = add("tail")
            cli("queue", "play")
            wait_for(
                lambda: (
                    cli("queue", "list")["running"]
                    and cli("queue", "list")["running"]["name"] == blocker
                )
            )
            scheduled = prefix + "-scheduled"
            future = p.datetime.now() + p.timedelta(days=2)
            cli(
                "job",
                "add",
                scheduled,
                "--calendar",
                json.dumps({"Month": future.month, "Day": future.day, "Hour": 0, "Minute": 0}),
                "--",
                PYTHON,
                str(script),
                str(root),
                "scheduled",
                "2",
                "0",
                "no",
            )
            cli("job", "run", scheduled)
            wait_for(lambda: p.jobs()[scheduled].get("pending"))
            wait_for(lambda: finished(tail), seconds=cfg.get("idle_seconds", 300) + 120)
            ordered = [
                e[0]
                for e in events()
                if e[1] == "start" and e[0] in ("blocker", "scheduled", "tail")
            ]
            assert ordered == ["blocker", "scheduled", "tail"], ordered
            print("PASS scheduled job takes the front")
        finally:
            current = cli("queue", "list")
            assert not current["running"] or current["running"]["name"] in added, (
                "another owner job is running; refusing to stop it"
            )
            cli("queue", "stop")
            if scheduled and scheduled in p.jobs():
                wait_for(lambda: not p.job_live(p.jobs()[scheduled]))
                cli("job", "remove", scheduled)
            for name in added:
                cli("queue", "remove", name)
    print("PASS cleanup; installed-copy queue acceptance complete")


if __name__ == "__main__":
    main()
