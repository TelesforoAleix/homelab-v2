"""Eval results are attempt aggregates, independent of the knowledge eval."""

from decimal import Decimal

from homelab import cli
from homelab.tasks import finish
from homelab.tasks.eval_results import results
from homelab.tasks.submit import parse, submit
from tests.test_task_store import connection, encoded, entry, task_file  # noqa: F401

ROUTE = {"kind": "litellm", "purpose": "task-worker"}


def test_bare_eval_still_runs_knowledge_eval(monkeypatch):
    calls = []
    monkeypatch.setattr("homelab.knowledge.evaluate.run_eval", lambda: calls.append(True) or 7)
    assert cli.main(["eval"]) == 7
    assert calls == [True]


def test_results_command_dispatches_without_knowledge_eval(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "homelab.tasks.eval_results.print_results", lambda name: calls.append(name) or 0
    )
    assert cli.main(["eval", "results", "--set", "T"]) == 0
    assert calls == ["T"]


def test_results_group_by_case_and_hash_without_multiplying_reviews(connection):  # noqa: F811
    mark = {"set": "T", "case": "example", "configuration": "base"}
    (task_id,) = submit(connection, parse(encoded(task_file(entry(1, eval=mark)))))
    for number, digest, cost, status in [
        (1, "a" * 64, 1.0, "done"),
        (2, "a" * 64, None, "failed"),
        (3, "b" * 64, 3.0, "done"),
    ]:
        finish.record(
            connection,
            finish.BeginAttempt(
                event="begin_attempt",
                task_id=task_id,
                number=number,
                route=ROUTE,
            ),
        )
        event = finish.EndAttempt(
            event="end_attempt",
            task_id=task_id,
            number=number,
            route=ROUTE,
            status=status,
            reason="eval_kept" if status == "done" else "check_failed",
            configuration_hash=digest,
            steps=2,
            fresh_tokens=10,
            cached_tokens=4,
            output_tokens=6,
            cost_usd=cost,
            review_route=ROUTE,
            reviews=[finish.Review(result="pass", steps=1, cost_usd=0.5)],
        )
        finish.record(connection, event)
        finish.record(connection, event)
    rows = results(connection, "T")
    assert len(rows) == 2
    assert rows[0][:4] == ("example", "a" * 64, 2, 1)
    assert rows[0][4:8] == (Decimal(3), Decimal(10), Decimal(4), Decimal(6))
    assert rows[0][9] is None
    assert rows[1][:4] == ("example", "b" * 64, 1, 1)
    assert rows[1][9] == Decimal("3.5")
    assert results(connection, "L") == []
