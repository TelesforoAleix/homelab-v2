import io
import json
from types import SimpleNamespace

from tests.test_copy import load


def test_host_notice_uses_existing_notifier_then_acknowledges(monkeypatch):
    helper = load("image_cost_notice", "node/usr/local/sbin/homelab-image-cost-notice")
    acknowledgements = io.StringIO()
    process = SimpleNamespace(
        stdout=io.StringIO(json.dumps({"message": "80% reached"}) + '\n{"done":true}\n'),
        stdin=acknowledgements,
        wait=lambda timeout: 0,
        poll=lambda: 0,
    )
    calls = []
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(helper.subprocess, "run", lambda args, **kw: calls.append(args))
    assert helper.main() == 0
    assert calls == [["/usr/local/sbin/homelab-notify.sh", "80% reached"]]
    assert acknowledgements.getvalue() == "sent\n"


def test_failed_notifier_is_not_acknowledged(monkeypatch):
    helper = load("image_cost_notice_failure", "node/usr/local/sbin/homelab-image-cost-notice")
    acknowledgements = io.StringIO()
    process = SimpleNamespace(
        stdout=io.StringIO(json.dumps({"message": "80% reached"}) + "\n"),
        stdin=acknowledgements,
        wait=lambda timeout: 1,
        poll=lambda: 0,
    )
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *a, **kw: process)

    def fail(*args, **kwargs):
        raise helper.subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(helper.subprocess, "run", fail)
    assert helper.main() == 1
    assert "sent" not in acknowledgements.getvalue()
