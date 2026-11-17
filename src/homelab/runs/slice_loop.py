"""Two CLI workers beside the independent serial ticket loop, under its lifetime locks."""

import logging
import os
import signal
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from homelab.runs import launcher, slices, usage
from homelab.tasks import slices as store
from homelab.tasks.executors import Executor, load_executors

logger = logging.getLogger("homelab-runs")


class Service:
    def __init__(self, database, root: Path, legacy_inactive):
        self.database, self.root, self.legacy_inactive = database, root, legacy_inactive
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="whole-run")
        self.thread = None
        self.table = load_executors()
        self.cache = usage.ClaudeCache(root / "claude-usage.json", self.probe)

    def probe(self):
        with tempfile.TemporaryDirectory(dir=self.root, prefix="usage-") as temporary:
            return slices.claude_probe(Path(temporary), cancelled=self.stop.is_set)

    def start(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.thread = threading.Thread(target=self.loop, daemon=True, name="whole-run-queue")
        self.thread.start()

    def loop(self):
        futures = set()
        next_sweep = 0
        try:
            while not self.stop.is_set():
                self.legacy_inactive()
                full_sweep = time.monotonic() >= next_sweep
                with self.database() as connection:
                    store.sweep(connection, failed_only=not full_sweep)
                if full_sweep:
                    next_sweep = time.monotonic() + 3600
                for future in list(futures):
                    if future.done():
                        future.result()
                        futures.remove(future)
                if len(futures) < 2:
                    with self.database() as connection:
                        saved = store.claim(connection)
                    if saved is not None:
                        futures.add(self.pool.submit(self.run, saved))
                        continue
                self.stop.wait(1)
        except BaseException as error:
            logger.error("whole-run queue stopped error=%s", type(error).__name__)
            if not self.stop.is_set():
                os.kill(os.getpid(), signal.SIGTERM)

    def run(self, saved):
        begun = False
        result, session, commits, metrics = None, saved["session_id"], [], {}
        with tempfile.TemporaryDirectory(dir=self.root, prefix="turn-") as temporary:
            folder = Path(temporary)
            try:
                choices = (
                    [Executor(**saved["executor"])]
                    if saved["harness_started"]
                    else self.table[saved["request"]["role"]][saved["request"]["level"]]
                )
                readings = {}

                def read(harness):
                    if harness not in readings:
                        try:
                            readings[harness] = (
                                slices.codex_usage(folder, cancelled=self.stop.is_set)
                                if harness == "codex"
                                else self.cache.read()
                            )
                        except launcher.Refused:
                            readings[harness] = None
                    return readings[harness]

                selected, reset = usage.choose(choices, read, time.time())
                if self.stop.is_set():
                    return
                if selected is None:
                    with self.database() as connection:
                        store.defer_usage(connection, saved, datetime.fromtimestamp(reset, UTC))
                    return
                with self.database() as connection:
                    store.begin(connection, saved, selected)
                    prompt = store.prompt(connection, saved)
                begun = True
                if not saved["copy_ready"]:
                    # Remove only our incomplete copy; prepared copies survive resumes.
                    work_id = "run-" + saved["id"].hex
                    launcher.request({"op": "cli-remove", "work_id": work_id})
                    with self.database() as connection:
                        store.prepared(connection, saved["id"], work_id, ready=False)
                    saved["work_id"] = slices.prepare(saved, folder, cancelled=self.stop.is_set)
                    with self.database() as connection:
                        store.prepared(connection, saved["id"], saved["work_id"])
                if self.stop.is_set():
                    return
                result, session, commits, metrics = slices.execute_turn(
                    saved, prompt, folder, saved["turn_id"], cancelled=self.stop.is_set
                )
                if result.get("status_reason") == "usage_limit":
                    try:
                        current = (
                            slices.codex_usage(folder, cancelled=self.stop.is_set)
                            if selected.harness == "codex"
                            else self.cache.read()
                        )
                    except launcher.Refused:
                        current = None
                    reset = (
                        current.reset
                        if current and current.reset > time.time()
                        else time.time() + 3600
                    )
                    result.update(slices.waiting_state(reset))
            except Exception as error:
                # Only exception classes and fixed status reasons enter logs.
                logger.warning("whole-run failed id=%s error=%s", saved["id"], type(error).__name__)
                result = {
                    "status": "failed",
                    "resumable": False,
                    "status_reason": str(error)
                    if isinstance(error, slices.Refused)
                    else "sandbox_error",
                }
                if not begun:
                    with self.database() as connection:
                        store.begin(connection, saved, saved["executor"])
            if not self.stop.is_set():
                with self.database() as connection:
                    store.complete(connection, saved, result, session, commits, metrics)
                if result["status"] in {"failed", "cancelled"}:
                    with self.database() as connection:
                        store.sweep(connection, failed_only=True)

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(5)
        launcher.stop_all()
        self.pool.shutdown(wait=True, cancel_futures=True)
