"""Outside-sandbox Mac acceptance: synthetic node probes, then restore real profiles.

Requires exact Working, AC, installed main tools, a migrated file, an idle med-ask,
and the gateway deployment. No app is quit and no privileged setting is changed.
"""

import copy
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path.home() / "Library/Application Support/homelab/mac"
COMMAND = str(TOOLS / "homelab-profile")
REGISTRY = Path.home() / ".config/homelab/models.json"
BACKUP = Path.home() / ".local/state/homelab-profile/model-acceptance-backup.json"
NODE = ["ssh", "homelab-agent", "sudo docker exec -i homelab-litellm-1 python -"]


def command(*args, value=None):
    result = subprocess.run(
        [COMMAND, *args],
        input=json.dumps(value) if value is not None else None,
        text=True,
        capture_output=True,
        check=True,
        timeout=900,
    )
    return json.loads(result.stdout)


def private_write(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value))
    temp.chmod(0o600)
    temp.replace(path)


def restore():
    backup = json.loads(BACKUP.read_text())
    # Restore the profile first while its original models are still registered.
    command("profiles-set", value=backup["profiles"])
    command("switch", "working")
    private_write(REGISTRY, backup["registry"])
    assert command("status")["profile"] == "working"
    BACKUP.unlink()
    print(json.dumps({"real_profiles_restored": True}), flush=True)


def node_probe(extra=False):
    # Reuse the synthetic image and sentences; do not run the older cold-start probe.
    source = (ROOT / "tests/mac_router_probe.py").read_text()
    program = "namespace = {'__name__': 'fixture'}\nexec(" + repr(source) + ", namespace)\n"
    program += """
import base64
import json
import math
import urllib.error
request = namespace['request']
base = 'http://127.0.0.1:4000'
image = base64.b64encode(namespace['image']()).decode()
payload = {'model': 'vision', 'max_tokens': 32, 'temperature': 0,
           'messages': [{'role': 'user', 'content': [
               {'type': 'text', 'text': 'Name the solid colour. Answer briefly.'},
               {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + image}}]}]}
answer = request(base, '/v1/chat/completions', payload)
assert 'red' in answer['choices'][0]['message']['content'].lower()
sentences = namespace['SENTENCES']
def vectors(model):
    response = request(base, '/v1/embeddings', {'model': model, 'input': sentences})
    values = [row['embedding'] for row in sorted(response['data'], key=lambda row: row['index'])]
    assert len(values) == len(sentences) and all(len(v) == 2560 for v in values)
    return values
node = vectors('embed-large')
def compare(model):
    mac = vectors(model)
    cosines = [sum(a*b for a,b in zip(left,right,strict=True)) /
               math.sqrt(sum(a*a for a in left)*sum(b*b for b in right))
               for left,right in zip(mac,node,strict=True)]
    assert min(cosines) >= 0.999
compare('embed-large-bulk')
print(json.dumps({'vision_identified_red': True,
                  'embedding_vector_space_preserved': True}), flush=True)
"""
    if extra:
        program += """
compare('embed-large-bulk-Qwen3-Embedding-4B')
compare('embed-large-bulk-acceptance-small')
try:
    request(base, '/v1/embeddings', {'model': 'embed-large-bulk-forbidden', 'input': 'test'})
except urllib.error.HTTPError as exc:
    assert exc.code == 503
    assert json.load(exc)['error']['message'] == 'embed-large-bulk is not available in this profile'
else:
    raise AssertionError('forbidden model answered')
print(json.dumps({'both_model_names_answered': True, 'unallowed_model_refused': True}), flush=True)
"""
    # Any exception is reduced to a type; no upstream diagnostics or private URLs.
    wrapped = "try:\n" + "\n".join("    " + line for line in program.splitlines())
    wrapped += (
        "\nexcept Exception as exc:\n    print(type(exc).__name__)"
        "\n    raise SystemExit(1) from None\n"
    )
    subprocess.run(NODE, input=wrapped, text=True, check=True, timeout=1800)


def cycle():
    assert not BACKUP.exists(), "pending recovery: run this script with restore first"
    assert command("status")["profile"] == "working", "return to exact Working before acceptance"
    power = subprocess.check_output(["/usr/bin/pmset", "-g", "batt"], text=True)
    assert "Now drawing from 'AC Power'" in power, "AC required"
    profiles = command("profiles-show")
    assert profiles["version"] == 2
    registry = json.loads(REGISTRY.read_text())
    private_write(BACKUP, {"profiles": profiles, "registry": registry})
    try:
        test = copy.deepcopy(profiles)
        policy = test["profiles"]["working"]["services"]
        policy["vision"]["state"] = "on request"
        policy["embed-large-bulk"]["state"] = "on request"
        command("profiles-set", value=test)
        command("switch", "working")
        node_probe()
        fixture = copy.deepcopy(registry)
        fixture["services"]["embed-large-bulk"]["acceptance-small"] = copy.deepcopy(
            fixture["services"]["embed-large-bulk"]["Qwen3-Embedding-4B"]
        )
        fixture["services"]["embed-large-bulk"]["acceptance-small"]["flags"]["c"] = "512"
        # Available in the registry, deliberately absent from the profile's allow-list.
        fixture["services"]["embed-large-bulk"]["forbidden"] = copy.deepcopy(
            fixture["services"]["embed-large-bulk"]["acceptance-small"]
        )
        private_write(REGISTRY, fixture)
        policy["embed-large-bulk"] = {
            "state": "on",
            "allowed": ["Qwen3-Embedding-4B", "acceptance-small"],
            "default": "acceptance-small",
        }
        command("profiles-set", value=test)
        status = command("switch", "working")
        assert status["services"]["embed-large-bulk"]["models"] == {
            "Qwen3-Embedding-4B": "loaded",
            "acceptance-small": "loaded",
        }
        print(json.dumps({"on_models_loaded_immediately_after_switch": True}), flush=True)
        node_probe(extra=True)
    finally:
        restore()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["cycle", "restore"])
    args = parser.parse_args()
    os.umask(0o077)
    try:
        restore() if args.action == "restore" else cycle()
    except Exception as exc:
        print(json.dumps({"failed": type(exc).__name__, "recovery_pending": BACKUP.exists()}))
        raise SystemExit(1) from None
