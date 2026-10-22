"""Owner-operated resumable Mac commands; all mutations use the profile command lock."""

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

LABEL = "org.homelab.profile.queue"
PROGRESS_ENV = "HOMELAB_QUEUE_PROGRESS"


class Queue:
    def __init__(self, host):
        self.h = host
        self.children = {}
        self.awake = []

    def call(self, name, *args):
        return self.h[name](*args)

    @property
    def root(self):
        return self.h["STATE"]

    def read(self):
        return self.call(
            "load",
            self.root / "queue.json",
            {"play": False, "paused_on_battery": False, "jobs": []},
        )

    def write(self, state):
        path = self.root / "queue.json"
        self.call("save", path, state)
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

    def running(self, state=None):
        return next((j for j in (state or self.read())["jobs"] if j["state"] == "running"), None)

    def active(self):
        # A claimed job remains activity until the worker has reaped and recorded it.
        return self.running() is not None

    def paths(self, name):
        return self.root / f"queue-{name}.log", self.root / f"queue-{name}.progress"

    def command(self, action, name=None, argv=None, cwd=None):
        state = self.read()
        jobs = state["jobs"]
        job = next((j for j in jobs if j["name"] == name), None)
        if action == "add":
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", name):
                raise ValueError("invalid job name")
            if job:
                raise ValueError("queue name already exists")
            if not argv or not Path(argv[0]).is_absolute():
                raise ValueError("absolute executable required")
            if cwd is not None and (not Path(cwd).is_absolute() or not Path(cwd).is_dir()):
                raise ValueError("cwd must be an existing absolute directory")
            log, progress = self.paths(name)
            for path in (log, progress):
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(fd)
            jobs.append(
                {
                    "name": name,
                    "argv": argv,
                    "cwd": cwd,
                    "state": "waiting",
                    "attempt": 0,
                    "log_path": str(log),
                    "progress_path": str(progress),
                }
            )
        elif action == "play":
            state["play"] = True
        elif action == "stop":
            was_running = self.running(state) is not None
            state["play"] = False
            self.write(state)  # Persist stop intent before signalling.
            self.stop(state)
            if was_running:
                self.call("dispatch_pending", self.call("config"))
        elif action in ("remove", "top", "requeue"):
            if job is None:
                raise ValueError("unknown queue job")
            allowed = {
                "remove": ("waiting", "failed", "finished"),
                "top": ("waiting",),
                "requeue": ("failed",),
            }
            stopped = job["state"] == "waiting" and job.get("stopped", False)
            if job["state"] not in allowed[action] and not (action == "requeue" and stopped):
                raise ValueError("queue operation not allowed in this state")
            jobs.remove(job)
            if action == "top":
                jobs.insert(0, job)
            elif action == "requeue":
                job.update(state="waiting", attempt=0, stopped=True)
                for key in ("exit_code", "started", "stopped"):
                    job.pop(key, None)
                jobs.append(job)
            else:
                self.write(state)
                for path in self.paths(name):
                    path.unlink(missing_ok=True)
        self.write(state)
        return self.report(state)

    def signal_group(self, pid, sig):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, sig)

    def group_alive(self, pid):
        # Ignore zombies: they cannot hold models or execute work.
        result = subprocess.run(
            ["/bin/ps", "-axo", "pgid=,stat="], capture_output=True, text=True, check=True
        )
        return any(
            int(parts[0]) == pid and not parts[1].startswith("Z")
            for line in result.stdout.splitlines()
            if len(parts := line.split()) == 2
        )

    def terminate(self, job):
        pid = job.get("pid")
        if not pid:
            return
        identity = self.call("router")["identity"](pid)
        if identity and identity != job.get("identity"):
            return  # The recorded PID has been reused; never signal its new group.
        self.signal_group(pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while self.group_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.group_alive(pid):
            self.signal_group(pid, signal.SIGKILL)
            killed = time.monotonic() + 2
            while self.group_alive(pid):
                if time.monotonic() >= killed:
                    raise TimeoutError("queue process group did not exit after SIGKILL")
                time.sleep(0.01)
        child = self.children.pop(pid, None)
        if child:
            child.wait()

    def stop(self, state):
        job = self.running(state)
        if job:
            job["stopping"] = True
            self.write(state)
            self.terminate(job)
            job.update(state="waiting", attempt=0, stopped=True)
            for key in ("pid", "identity", "started", "stopping"):
                job.pop(key, None)
            state["jobs"].remove(job)
            state["jobs"].insert(0, job)
            self.write(state)

    def power(self, state):
        now = time.time()
        ac = self.call("on_ac_power")
        source = "ac" if ac else "battery"
        changed = state.get("power_source") != source
        if changed:
            state.update(power_source=source, power_since=now)
        if (
            not ac
            and not state["paused_on_battery"]
            and now - state["power_since"] >= self.h["BATTERY_GRACE_SECONDS"]
        ):
            state["paused_on_battery"] = True
            self.write(state)
            self.stop(state)
        if (
            ac
            and state["paused_on_battery"]
            and now - state["power_since"] >= self.h["BATTERY_GRACE_SECONDS"]
        ):
            state["paused_on_battery"] = False
            changed = True
        if changed:
            self.write(state)
        return ac and not state["paused_on_battery"]

    def scheduled_waiting(self):
        return any(j.get("pending") or self.call("job_live", j) for j in self.call("jobs").values())

    def start(self, job, state):
        Path(job["progress_path"]).write_text("")
        job.pop("stopped", None)
        job.update(state="running", started=time.time(), attempt=job["attempt"] + 1)
        # Intent is durable before spawn; recovery treats an interrupted attempt as waiting.
        self.write(state)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        reader, writer = os.pipe()
        try:
            with open(job["log_path"], "ab", buffering=0) as output:
                child = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), str(reader), *job["argv"]],
                    pass_fds=(reader,),
                    cwd=job["cwd"],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask),
                    env={
                        **os.environ,
                        "HOMELAB_PROFILE_JOB": "1",
                        PROGRESS_ENV: job["progress_path"],
                    },
                )
            self.children[child.pid] = child
            job.update(pid=child.pid, identity=self.call("router")["identity"](child.pid))
            self.write(state)
            self.awake.append(
                subprocess.Popen(
                    ["/usr/bin/caffeinate", "-is", "-w", str(child.pid)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
            os.write(writer, b"1")  # Run only after the PID and its identity are durable.
        except OSError:
            if not job.get("pid"):
                self.finish(job, 127)
                self.write(state)
            else:
                raise
        finally:
            os.close(reader)
            os.close(writer)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)

    def finish(self, job, code):
        job.update(
            exit_code=code,
            state="finished" if code == 0 else "waiting" if job["attempt"] < 2 else "failed",
        )
        for key in ("pid", "identity", "stopping"):
            job.pop(key, None)

    def step(self):
        """One locked worker iteration. Child handles exist only in the launchd worker."""
        self.awake[:] = [process for process in self.awake if process.poll() is None]
        for pid, child in list(self.children.items()):
            if child.poll() is not None and not any(
                j.get("pid") == pid for j in self.read()["jobs"]
            ):
                del self.children[pid]
        state = self.read()
        ready = self.power(state)
        job = self.running(state)
        if job and (job.get("stopping") or not state["play"]):
            self.stop(state)
            job = None
        if job:
            child = self.children.get(job.get("pid"))
            if child is None:
                # Worker restart: stop its orphaned attempt before allowing another command.
                self.stop(state)
                return
            code = child.poll()
            if code is None:
                return
            # A command may leave descendants; release the entire group before the next job.
            self.terminate(job)
            self.finish(job, code)
            if job["state"] == "waiting":
                state["jobs"].remove(job)
                state["jobs"].insert(0, job)
            self.write(state)
            self.call("dispatch_pending", self.call("config"))
        if not state["play"] or not ready or self.scheduled_waiting():
            return
        job = next((j for j in state["jobs"] if j["state"] == "waiting"), None)
        if job:
            self.start(job, state)

    def memory(self, pid=None):
        resident = 0
        free = None
        with contextlib.suppress(OSError, ValueError, TypeError, subprocess.SubprocessError):
            rows = subprocess.check_output(["/bin/ps", "-axo", "pgid=,rss="], text=True)
            resident = (
                sum(
                    int(parts[1])
                    for line in rows.splitlines()
                    if len(parts := line.split()) == 2 and int(parts[0]) == pid
                )
                / 1024
            )
            vm = subprocess.check_output(["/usr/bin/vm_stat"], text=True)
            page = int(re.search(r"page size of (\d+) bytes", vm)[1])
            free = int(re.search(r"Pages free:\s+(\d+)", vm)[1]) * page / (1024 * 1024)
        return resident, free

    def report(self, state=None):
        state = state or self.read()
        job = self.running(state)
        resident, free = self.memory(job.get("pid") if job else None)
        running = None
        if job:
            progress = ""
            with contextlib.suppress(OSError):
                with open(job["progress_path"], errors="replace") as handle:
                    progress = handle.readline(1024).strip()
            running = {
                **job,
                "elapsed_seconds": max(0, time.time() - job["started"]),
                "progress": progress,
                "resident_memory_mb": resident,
            }
        return {
            "play": state["play"],
            "paused_on_battery": state["paused_on_battery"],
            "running": running,
            "free_memory_mb": free,
            **{
                kind: [j for j in state["jobs"] if j["state"] == kind]
                for kind in ("waiting", "failed", "finished")
            },
        }

    def worker(self):
        if self.call("under_vscode") or os.getppid() != 1:
            raise ValueError("queue worker must run through launchd, outside VS Code")

        def interrupted(signum, frame):
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, interrupted)
        try:
            while True:
                with self.call("lock"):
                    self.step()
                time.sleep(1)
        finally:
            with self.call("lock"):
                self.stop(self.read())


if __name__ == "__main__":
    # A parent crash before acknowledgement closes the pipe: no unrecorded command runs.
    gate = int(sys.argv[1])
    acknowledged = os.read(gate, 1) == b"1"
    os.close(gate)
    if acknowledged:
        try:
            os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
        except OSError:
            raise SystemExit(127) from None
