"""Home Lab's typed choices, with a private Jev adapter and content-free accounting."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Literal

import httpx
import psycopg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from homelab.api.image_costs import connect, month_at
from homelab.api.run_keys import authorize
from homelab.tasks.store import RunError

router = APIRouter()
Name = Annotated[str, Field(min_length=1)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Question(Strict):
    instructions: str
    choices: dict[Name, str] = Field(min_length=1)


class DecisionRequest(Strict):
    text: dict[Name, str] = Field(min_length=1)
    questions: dict[Name, Question] = Field(min_length=1)
    repeats: int = Field(default=1, ge=1, le=5)


class Answer(Strict):
    choice: str
    confidence: Probability
    probabilities: dict[str, Probability]


class QuestionResult(Answer):
    agreement: Probability
    answers: list[Answer]


class Usage(Strict):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class DecisionResponse(Strict):
    provider: Literal["typesafe"] = "typesafe"
    model: str
    questions: dict[str, QuestionResult]
    usage: Usage


class JevAnswer(Answer):
    type: Literal["choice"]


class JevResponse(Strict):
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    answers: dict[str, JevAnswer]
    usage: Usage


def migrate():
    with connect() as connection:
        connection.execute(Path(__file__).with_name("decisions.sql").read_text())


def now_utc():
    return datetime.now(UTC)


def configuration():
    try:
        budget = Decimal(os.environ["HOMELAB_DECISIONS_MONTHLY_BUDGET_USD"])
        price = Decimal(os.environ["HOMELAB_JEV_USD_PER_MILLION_INPUT_TOKENS"])
        if not budget.is_finite() or budget <= 0 or not price.is_finite() or price < 0:
            raise ValueError
        return budget, price
    except (KeyError, InvalidOperation, ValueError):
        raise RunError(503, "decisions_unavailable") from None


def provider_client(*, transport=None):
    return httpx.Client(timeout=30, follow_redirects=False, trust_env=False, transport=transport)


def provider_call(client: httpx.Client, key: str, body: DecisionRequest) -> JevResponse:
    try:
        response = client.post(
            "https://api.typesafe.ai/v1/systemone",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": "jev-latest",
                "state": body.text,
                "questions": {
                    name: {
                        "type": "choice",
                        "instructions": question.instructions,
                        "criteria": question.choices,
                    }
                    for name, question in body.questions.items()
                },
            },
        )
        if response.status_code == 400:
            kind = response.json()["detail"]["error_type"]
            # Provider messages and arbitrary diagnostics may contain content or keys.
            if not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", kind):
                raise ValueError
            if key in kind:
                raise ValueError
            if kind == "max_tokens_exceeded":
                raise RunError(413, "decisions_input_too_large", status_reason=kind)
            raise RunError(422, kind)
        if response.status_code != 200:
            raise ValueError
        result = JevResponse.model_validate(response.json())
        if key in result.model or set(result.answers) != set(body.questions):
            raise ValueError
        for name, answer in result.answers.items():
            choices = body.questions[name].choices
            if answer.choice not in choices or set(answer.probabilities) != set(choices):
                raise ValueError
        return result
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise RunError(502, "decisions_provider_error") from None


def aggregate(body: DecisionRequest, results: list[JevResponse]) -> dict[str, QuestionResult]:
    questions = {}
    for name, question in body.questions.items():
        answers = [
            Answer(**result.answers[name].model_dump(exclude={"type"})) for result in results
        ]
        probabilities = {
            choice: sum(answer.probabilities[choice] for answer in answers) / len(answers)
            for choice in question.choices
        }
        counts = Counter(answer.choice for answer in answers)
        # Exact ties after mean probability use the request's choice order.
        choice = max(question.choices, key=lambda value: (counts[value], probabilities[value]))
        questions[name] = QuestionResult(
            choice=choice,
            probabilities=probabilities,
            confidence=sum(answer.confidence for answer in answers) / len(answers),
            agreement=counts[choice] / len(answers),
            answers=answers,
        )
    return questions


def decide(application: str, body: DecisionRequest):
    results = []
    usage = Usage(input_tokens=0, output_tokens=0)
    model = None
    cost = Decimal(0)
    status = "ok"
    with connect() as connection:
        # Serialize admission through recording, including across API processes. A
        # crossing call can finish; the next call observes its committed charge.
        connection.execute("SELECT pg_advisory_xact_lock(157443, 1)")
        now = now_utc()
        month = month_at(now)
        next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        try:
            if sum(map(len, body.text.values())) > 80_000:
                status = "max_tokens_exceeded"
                raise RunError(413, "decisions_input_too_large")
            key = os.environ.get("HOMELAB_TYPESAFE_API_KEY", "")
            status = "unavailable"
            if not key.strip():
                raise RunError(503, "decisions_unavailable")
            budget, price = configuration()
            total = connection.execute(
                """SELECT COALESCE(SUM(cost_usd), 0) FROM homelab_decision_calls
                WHERE called_at >= %s AND called_at < %s""",
                (
                    datetime.combine(month, datetime.min.time(), UTC),
                    datetime.combine(next_month, datetime.min.time(), UTC),
                ),
            ).fetchone()[0]
            if total >= budget:
                status = "refused_budget"
                raise RunError(429, "decisions_budget_reached")
            with provider_client() as client:
                for _ in range(body.repeats):
                    try:
                        result = provider_call(client, key, body)
                    except RunError as error:
                        status = error.status_reason or (
                            error.code if error.status == 422 else "provider_error"
                        )
                        raise
                    results.append(result)
                    model = result.model
                    usage.input_tokens += result.usage.input_tokens
                    usage.output_tokens += result.usage.output_tokens
                    cost = Decimal(usage.input_tokens) * price / 1_000_000
            content = DecisionResponse(
                model=model, questions=aggregate(body, results), usage=usage
            ).model_dump()
            status_code = 200
            status = "ok"
        except RunError as error:
            content = {"error": error.code}
            status_code = error.status
        connection.execute(
            """INSERT INTO homelab_decision_calls
            (called_at, application, provider, model, question_count, repeats,
             input_tokens, output_tokens, cost_usd, status)
            VALUES (%s, %s, 'typesafe', %s, %s, %s, %s, %s, %s, %s)""",
            (
                now,
                application,
                model,
                len(body.questions),
                body.repeats,
                usage.input_tokens,
                usage.output_tokens,
                cost,
                status,
            ),
        )
    return JSONResponse(content, status_code=status_code)


@router.post("/v1/decisions", response_model=DecisionResponse)
async def post_decisions(request: Request):
    try:
        identity = authorize(request, "decisions:write")
        try:
            body = DecisionRequest.model_validate(await request.json())
        except (ValidationError, json.JSONDecodeError, UnicodeDecodeError):
            raise RunError(422, "invalid_request") from None
        return await run_in_threadpool(decide, identity.application, body)
    except RunError as error:
        return JSONResponse({"error": error.code}, status_code=error.status)
    except psycopg.Error:
        return JSONResponse({"error": "decisions_unavailable"}, status_code=503)
