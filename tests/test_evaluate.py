import json
from types import SimpleNamespace

import pytest

from homelab import cli
from homelab.knowledge import evaluate as module
from homelab.settings import Settings


def item(id_, expect="answer", group="hiring"):
    return dict(
        id=id_,
        question=f"Synthetic private question {id_}?",
        expect=expect,
        entries=["entry-a", "entry-b"] if expect == "answer" else [],
        group=group,
    )


def write_eval(tmp_path, sets):
    path = tmp_path / "eval.json"
    path.write_text(json.dumps(dict(version=1, created="2026-10-02", scoring={}, sets=sets)))
    return path


def test_scoring_and_groups():
    items = [item(str(n)) for n in range(6)] + [
        item("6", "refuse", "scope"),
        item("7", "refuse", "scope"),
    ]
    retrieved = [
        ["other", "entry-b", "entry-a"],
        ["entry-a"],
        ["entry-a"],
        ["other"] * 5 + ["entry-a"],
        [],
        ["entry-b"],
    ]
    responses = [
        (False, ["entry-b"]),
        (True, []),
        (False, ["other"]),
        (False, ["entry-a"]),
        (True, []),
        (False, []),
        (True, []),
        (False, ["other"]),
    ]
    calls = []

    def retriever(question, top_k):
        assert top_k == 5
        n = int(question.split()[-1][:-1])
        return [SimpleNamespace(path=path) for path in retrieved[n]]

    def answerer(question, top_k):
        assert top_k == 5
        calls.append(question)
        n = int(question.split()[-1][:-1])
        refused, paths = responses[n]
        return SimpleNamespace(
            refused=refused,
            sources=[SimpleNamespace(path=path) for path in paths],
            answer="Synthetic private answer",
        )

    report = module.evaluate_sets({"visitor": items}, retriever=retriever, answerer=answerer)
    visitor = report["visitor"]
    assert visitor["answer"] == {
        "count": 6,
        "hit@5": 4 / 6,
        "MRR": 3.5 / 6,
        "answer_correct_rate": 2 / 6,
        "failures": [
            {"id": "1", "reasons": ["refused"]},
            {"id": "2", "reasons": ["wrong_citation"]},
            {"id": "3", "reasons": ["retrieval_miss"]},
            {"id": "4", "reasons": ["retrieval_miss", "refused"]},
            {"id": "5", "reasons": ["wrong_citation"]},
        ],
    }
    assert visitor["refuse"] == {
        "count": 2,
        "correct_refusal_rate": 0.5,
        "failures": [{"id": "7", "reason": "answered"}],
    }
    assert visitor["groups"]["hiring"]["answer"] == visitor["answer"]
    assert visitor["groups"]["scope"]["refuse"] == visitor["refuse"]
    assert visitor["groups"]["tech"]["answer"]["count"] == 0
    assert visitor["groups"]["hiring"]["refuse"]["correct_refusal_rate"] == 0
    assert len(calls) == len(items)
    assert "Synthetic private" not in json.dumps(report)


@pytest.mark.parametrize("exists", [False, True])
def test_cli_eval_file_and_content_safe_report(tmp_path, monkeypatch, capsys, exists):
    corpus = tmp_path / "corpus.json"
    corpus.write_text("[]")
    sets = {
        "bank_paraphrases": [item("A")],
        "refusal_checks": [item("R", "refuse")],
        "visitor": [item("V", group="tech")],
    }
    path = write_eval(tmp_path, sets) if exists else tmp_path / "missing.json"
    monkeypatch.setattr(
        module,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            corpus_file=corpus,
            eval_file=path,
        ),
    )
    monkeypatch.setattr("homelab.api.app.query_brain", lambda *a: [SimpleNamespace(path="entry-b")])
    monkeypatch.setattr(
        "homelab.api.app.answer_knowledge",
        lambda body: SimpleNamespace(
            refused=" R?" in body.question,
            sources=[SimpleNamespace(path="entry-b")],
            answer="Synthetic private answer",
        ),
    )
    assert cli.main(["eval"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["eval_file_found"] == int(exists)
    assert report["documents"] == report["refusal_count"] == 0
    assert "Synthetic private" not in captured.out + captured.err
    if exists:
        assert set(report["sets"]) == set(sets)
        assert report["sets"]["bank_paraphrases"]["answer"]["answer_correct_rate"] == 1
        assert report["sets"]["refusal_checks"]["refuse"]["correct_refusal_rate"] == 1
    else:
        assert "sets" not in report


@pytest.mark.parametrize(
    "payload",
    [
        '{"Synthetic private question":',
        json.dumps({"version": 2, "sets": {}}),
        json.dumps(
            {
                "version": 1,
                "sets": {
                    "bank_paraphrases": [dict(item("A"), entries=[])],
                    "refusal_checks": [],
                    "visitor": [],
                },
            }
        ),
    ],
)
def test_invalid_file_errors_are_content_safe(tmp_path, payload):
    path = tmp_path / "eval.json"
    path.write_text(payload)
    with pytest.raises(ValueError) as error:
        module.load_eval_sets(path)
    assert "Synthetic private" not in str(error.value)


def test_eval_setting_default_and_environment(monkeypatch):
    assert str(Settings(_env_file=None).eval_file) == "/data/corpus/about-aleix/eval.json"
    monkeypatch.setenv("HOMELAB_EVAL_FILE", "/tmp/synthetic-eval.json")
    assert str(Settings(_env_file=None).eval_file) == "/tmp/synthetic-eval.json"
