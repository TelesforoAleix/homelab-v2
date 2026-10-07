"""Synthetic scoring and local-only benchmark safety contracts."""

import pytest

from mac.vision_bench import local_url, normalise, outside_repository, score, summary


def test_normalisation_casefolds_keeps_accents_and_real_hyphens():
    assert normalise("  Árbol\tco-\n  operar\r\n A-B  ") == "árbol cooperar a-b"
    assert normalise("cafe\u0301\u00ad") == "café"
    assert normalise("a -\nb") == "a - b"


@pytest.mark.parametrize(
    ("reference", "output", "ce", "we"),
    [
        ("Cat dog", "Cat dog", 0, 0),
        ("Cat dog", "cat dogs", 1, 1),
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
    weights = tmp_path / "weights"
    projector = tmp_path / "projector"
    weights.write_bytes(b"123")
    projector.write_bytes(b"45")
    args = SimpleNamespace(
        server_url="http://127.0.0.1",
        server_binary="synthetic",
        dpi=150,
        context=100,
        timeout=1,
        load_timeout=0.001,
    )
    result = bench.run_model(
        args, ["synthetic", str(weights), str(projector)], [], tmp_path / "numbers.json", None
    )
    assert child.terminated
    assert result["mapped_weights_bytes"] == 5
    assert result["peak_memory_bytes"] == 105
    assert result["startup_failure"] == 1

    def numeric(value):
        if isinstance(value, dict):
            return all(numeric(v) for v in value.values())
        if isinstance(value, list):
            return all(numeric(v) for v in value)
        return value is None or isinstance(value, (int, float))

    assert numeric(json.loads((tmp_path / "numbers.json").read_text()))


def test_word_bag_is_order_insensitive_and_counts_duplicates():
    sequential = score("Alpha beta gamma delta", "alpha beta gamma delta")
    interleaved = score("Alpha beta gamma delta", "alpha gamma beta delta")
    for key in ("recall", "precision", "f1"):
        assert sequential[key] == interleaved[key] == 1
    result = score("cat cat dog", "cat dog dog extra")
    assert result["recall"] == pytest.approx(2 / 3)
    assert result["precision"] == 0.5
    assert result["f1"] == pytest.approx(4 / 7)
    assert score("Ａ co-\noperate café", "a cooperate CAFE\u0301")["f1"] == 1
    assert score("cat", "")["f1"] == 0


@pytest.mark.parametrize(
    ("text", "invalid"),
    [
        ("one two three four 12", 0),
        ("one two three 12 34", 1),
        ("‘café’ árbol; words.", 0),
        ("", 1),
        ("123 !!! 4/5", 1),
    ],
)
def test_reference_quality(text, invalid):
    from mac.vision_bench import reference_quality

    assert reference_quality(text)["invalid_reference"] == invalid


def test_invalid_references_are_excluded_from_aggregates():
    rows = [
        dict(
            score("word", "word"),
            failure=0,
            seconds=1,
            prompt_tokens=1,
            completion_tokens=1,
            invalid_reference=0,
        ),
        dict(
            score("123", "other"),
            failure=0,
            seconds=1,
            prompt_tokens=1,
            completion_tokens=1,
            invalid_reference=1,
        ),
    ]
    result = summary(rows)
    assert result["f1"] == 1
    assert result["scored"] == result["invalid_references"] == 1


def test_reconciliation_request_has_one_image_and_both_texts(tmp_path):
    import json

    import httpx

    from mac.vision_bench import RECONCILE_PROMPT, reconciliation_prompt, request

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "000.text.txt").write_text("Synthetic A")
    (b / "000.text.txt").write_text("Synthetic B")
    prompt = reconciliation_prompt(a, b, 0)

    def respond(req):
        content = json.loads(req.content)["messages"][0]["content"]
        assert len(content) == 2
        assert content[0]["text"] == (
            RECONCILE_PROMPT + "\n\nTranscription A\nSynthetic A\n\nTranscription B\nSynthetic B"
        )
        assert content[1]["type"] == "image_url"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Synthetic merged"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    with httpx.Client(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(respond)
    ) as client:
        assert request(client, "synthetic", b"png", prompt, 4)[0] == "Synthetic merged"
    with pytest.raises(FileNotFoundError):
        reconciliation_prompt(a, b, 1)


def test_invalid_reference_still_transcribes_without_figure_call(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import httpx
    import pymupdf

    import mac.vision_bench as bench

    pdf = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), "123 !!! 456")
        doc.save(pdf)
    weights, projector = tmp_path / "weights", tmp_path / "projector"
    weights.write_bytes(b"123")
    projector.write_bytes(b"45")

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
    calls = []

    def respond(req):
        if req.url.path == "/health":
            if not calls:
                calls.append("probe")
                raise httpx.ConnectError("synthetic", request=req)
            return httpx.Response(200)
        calls.append("transcribe")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Synthetic output"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    client = httpx.Client(base_url="http://127.0.0.1", transport=httpx.MockTransport(respond))
    monkeypatch.setattr(bench.httpx, "Client", lambda **kw: client)
    monkeypatch.setattr(bench.subprocess, "Popen", lambda *a, **kw: child)
    monkeypatch.setattr(bench, "footprint", lambda pid: 100)
    args = SimpleNamespace(
        server_url="http://127.0.0.1",
        server_binary="synthetic",
        dpi=150,
        context=16384,
        timeout=600,
        load_timeout=300,
        max_tokens=4096,
        prompt="exact",
        reconcile=None,
        no_figures=True,
    )
    output = tmp_path / "output"
    output.mkdir()
    result = bench.run_model(
        args,
        ["synthetic", str(weights), str(projector)],
        [[str(pdf), "1", "en", "figure", "score"]],
        tmp_path / "numbers.json",
        output,
    )
    assert child.terminated
    assert calls == ["probe", "transcribe"]
    assert result["pages"][0]["invalid_reference"] == 1
    assert result["pages"][0]["failure"] == 0
    assert result["summary"]["scored"] == 0
    assert (output / "000.text.txt").read_text() == "Synthetic output"
