# Decisions API

`POST http://127.0.0.1:8000/v1/decisions` makes typed choices behind Home Lab's contract.
Send `Content-Type: application/json` and `Authorization: Bearer <run-key>`. The existing
[run-key file](run-api.md#key-file-and-repository-policy) must grant `decisions:write`.
Repository permissions do not apply to decisions; a decisions-only key may have an empty
`repositories` map. No gateway, provider or environment key authorizes a client.

## Request

```json
{
  "text": {"change": "Update a documentation link."},
  "questions": {
    "risk": {
      "instructions": "Choose the review risk for this change.",
      "choices": {"low": "Routine change", "high": "Needs careful review"}
    }
  },
  "repeats": 3
}
```

`text` is a nonempty object of named strings shared by all questions. `questions` is a
nonempty object of named questions, each with string `instructions` and a nonempty `choices`
map from choice to string meaning. Names and choice labels must be nonempty strings. Extra
fields are rejected. `repeats` is an integer from 1 to 5, default 1; booleans and strings are
not integers. The combined character count of the `text` values may be at most 80,000
(Unicode characters, not encoded bytes). Instructions and meanings are also sent to the
provider, whose token limit may refuse a request below this character limit.

Clients supply no provider, model or provider request fields. The API currently sends the
text and questions to TypeSafe's hosted Jev model. A local implementation can replace that
adapter without changing this contract. Calls are sequential, with one 30-second HTTP timeout
per repeat, no automatic retries, no redirects and no environment proxy. A failure stops the
remaining repeats and returns an error instead of a partial result.

## Response

HTTP 200 returns named `questions`, the provider, the reported model and summed token usage:

```json
{
  "provider": "typesafe",
  "model": "jev-1.14.0",
  "questions": {
    "risk": {
      "choice": "low",
      "probabilities": {"low": 0.8, "high": 0.2},
      "confidence": 0.9,
      "agreement": 1.0,
      "answers": [
        {"choice": "low", "confidence": 0.9, "probabilities": {"low": 0.8, "high": 0.2}},
        {"choice": "low", "confidence": 0.9, "probabilities": {"low": 0.8, "high": 0.2}},
        {"choice": "low", "confidence": 0.9, "probabilities": {"low": 0.8, "high": 0.2}}
      ]
    }
  },
  "usage": {"input_tokens": 1152, "output_tokens": 120}
}
```

`choice` is the most frequent raw choice. Ties go to the choice with the higher mean
probability; an exact tie after that uses the request's choice order. `probabilities` is the
arithmetic mean for every offered choice. `confidence` is the arithmetic mean of all repeats'
confidence, and `agreement` is the fraction that chose the aggregate choice. `answers` retains
each repeat's raw choice, confidence and probabilities in call order. With one repeat,
agreement is 1. Confidence and probabilities are finite numbers in [0, 1]. Malformed provider
answers, missing questions or probabilities, and unoffered choices fail closed as provider
errors. The model is the final successful repeat's reported model if a version changes
between repeats. Usage sums input and output tokens across all repeats and questions.

## Cap and accounting

These variables are passed only to the `api` service by Compose:

| Variable | Compose default | Meaning |
|---|---|---|
| `HOMELAB_TYPESAFE_API_KEY` | empty | Provider bearer key, supplied by the owner in the node's private `.env` |
| `HOMELAB_DECISIONS_MONTHLY_BUDGET_USD` | `20` | Positive finite decimal monthly cap in USD |
| `HOMELAB_JEV_USD_PER_MILLION_INPUT_TOKENS` | `0.042` | Non-negative finite decimal USD per million input tokens; output is free |

Jev reports no cost. Recorded cost is `input_tokens × configured_price / 1,000,000`, using
decimal arithmetic. The price is a configured estimate from third-party listings, not a
provider billing receipt. The cap totals recorded decisions cost across all applications in
the current UTC calendar month, independently of the image/video gateway budget. A call is
refused when that total is already at or above the cap. A crossing call completes; the next
call is refused. A database transaction advisory lock serializes admission through recording
across API processes. Waiting calls check the month after acquiring the lock. Repeats belong
to one admitted call, so their combined charge can cross the cap. The new month starts at
00:00 UTC on its first day, without deleting earlier records.

API startup applies `decisions.sql`, creating only `homelab_decision_calls` and its time index.
Each authorized, structurally valid call writes one row, including local size, budget and
availability refusals. The row holds time, authenticated application, provider, reported model
(null before any successful repeat), question count, requested repeats, known summed input
and output tokens, decimal USD cost and status. Status is `ok`, a Jev error kind,
`refused_budget`, `unavailable`, or `provider_error` for transport/malformed-response failures.
The local size refusal uses `max_tokens_exceeded`. If a later repeat fails, known usage and
cost from earlier successful repeats are retained in that single failure row. A timeout can
leave an unknown provider charge; the API does not guess tokens or retry paid work.

Rows and application logs never contain text, questions, answers, bearer keys or provider
messages. Auth and structural validation failures do not reach accounting or Jev. Database
failure refuses the call with 503; if the database cannot write, there can be no accounting
row. A crash or database failure after a paid call can require manual billing reconciliation.
The provider key is never returned or logged.

## Errors

Errors contain only `{"error": "<code>"}`; provider messages and validation input are omitted.

| HTTP | Code | Meaning |
|---|---|---|
| 401 | `bearer_key_required` | Missing or unrecognized run key |
| 403 | `scope_required` | Key lacks `decisions:write` |
| 503 | `run_keys_unavailable` | Existing run-key authorization is unavailable |
| 422 | `invalid_request` | Invalid JSON, types, shape or repeats |
| 503 | `decisions_unavailable` | Missing provider key, invalid budget/price configuration, or unavailable accounting database |
| 429 | `decisions_budget_reached` | Current UTC month's recorded cost has reached the cap |
| 413 | `decisions_input_too_large` | Over 80,000 text characters locally, or Jev `max_tokens_exceeded` |
| 422 | Jev error kind, e.g. `api_usage_error` | Other Jev HTTP 400 refusal |
| 502 | `decisions_provider_error` | Timeout, connection failure, HTTP 5xx, unexpected status or malformed provider reply |

After merge and owner approval: set the provider key, grant the caller `decisions:write`,
pull the merged tree with `git pull --ff-only` in `/srv/homelab/homelab-v2`, and restart
`homelab.service` to rebuild the platform. The owner checks one real call and one refusal
under a temporary tiny cap, then restores the intended cap and restarts.
