"""Synthetic scoring and local-only benchmark safety contracts."""

import pytest

from mac.vision_bench import local_url, normalise, outside_repository, score, summary


def test_normalisation_keeps_case_accents_and_real_hyphens():
    assert normalise("  Árbol\tco-\n  operar\r\n A-B  ") == "Árbol cooperar A-B"
    assert normalise("cafe\u0301\u00ad") == "café"
    assert normalise("a -\nb") == "a - b"


@pytest.mark.parametrize(
    ("reference", "output", "ce", "we"),
    [
        ("Cat dog", "Cat dog", 0, 0),
        ("Cat dog", "cat dogs", 2, 2),
        ("a b", "a", 2, 1),
        ("a", "a b c", 4, 2),
        ("abc", "", 3, 1),
    ],
)
def test_edit_distance(reference, output, ce, we):
    result = score(reference, output)
    assert result["char_errors"] == ce
    assert result["word_errors"] == we
    assert result["cer"] == ce / len(reference)
    assert result["wer"] == we / len(reference.split())


def test_empty_reference_is_unscorable():
    with pytest.raises(ValueError):
        score(" \n", "anything")


def test_summary_uses_corpus_weighting_and_excludes_failures():
    rows = [
        dict(score("a", "b"), failure=0, seconds=1, prompt_tokens=10, completion_tokens=2),
        dict(score("abc", "abc"), failure=0, seconds=3, prompt_tokens=20, completion_tokens=4),
        dict(failure=1, seconds=10, prompt_tokens=0, completion_tokens=0),
    ]
    result = summary(rows)
    assert result["cer"] == 0.25
    assert result["wer"] == 0.5
    assert result["failures"] == 1
    assert result["median_seconds"] == 3
    assert result["p95_seconds"] == 10
    assert result["mean_prompt_tokens"] == 15


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8099",
        "http://localhost:8099",
        "http://example.org",
        "http://127.0.0.1/v1",
        "http://user:pass@127.0.0.1",
    ],
)
def test_remote_or_ambiguous_endpoints_refused(url):
    with pytest.raises(ValueError):
        local_url(url)


def test_loopback_url():
    assert local_url("http://127.0.0.1:8099") == 8099


def test_private_path_guard_resolves_symlinks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    with pytest.raises(ValueError):
        outside_repository(link / "private.txt")
    assert outside_repository(tmp_path / "private.txt") == tmp_path / "private.txt"


def test_image_request_contract():
    import json

    import httpx

    from mac.vision_bench import TEXT_PROMPT, request

    def respond(req):
        body = json.loads(req.content)
        assert req.url.path == "/v1/chat/completions"
        assert body["cache_prompt"] is False
        assert body["temperature"] == 0
        assert body["messages"][0]["content"][0]["text"] == TEXT_PROMPT
        assert body["messages"][0]["content"][1]["image_url"]["url"] == "data:image/png;base64,cG5n"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "Synthetic output"}, "finish_reason": "length"}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 4},
            },
        )

    with httpx.Client(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(respond)
    ) as client:
        output, usage, finish = request(client, "synthetic", b"png", TEXT_PROMPT, 4)
    assert output == "Synthetic output"
    assert usage["completion_tokens"] == 4
    assert finish == "length"


def test_startup_failure_stops_child_and_saves_only_numbers(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    import httpx

    import mac.vision_bench as bench

    class Child:
        pid = 123
        terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    child = Child()

    def unavailable(req):
        raise httpx.ConnectError("synthetic", request=req)

    client = httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(unavailable))
    monkeypatch.setattr(bench.httpx, "Client", lambda **kw: client)
    monkeypatch.setattr(bench.subprocess, "Popen", lambda *a, **kw: child)
    monkeypatch.setattr(bench, "footprint", lambda pid: 100)
    args = SimpleNamespace(
        server_url="http://127.0.0.1",
        server_binary="synthetic",
        dpi=150,
        context=100,
        timeout=1,
        load_timeout=0.001,
    )
    result = bench.run_model(
        args, ["synthetic", "weights", "projector"], [], tmp_path / "numbers.json", None
    )
    assert child.terminated
    assert result["startup_failure"] == 1

    def numeric(value):
        if isinstance(value, dict):
            return all(numeric(v) for v in value.values())
        if isinstance(value, list):
            return all(numeric(v) for v in value)
        return value is None or isinstance(value, (int, float))

    assert numeric(json.loads((tmp_path / "numbers.json").read_text()))
