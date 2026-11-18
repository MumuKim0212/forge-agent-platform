"""a request-validation error says which field failed and which rule, never what the caller sent.

FastAPI's default 422 puts pydantic's `input` into every entry. For a password that fails a rule the
echo is the obvious one (a 6-character `password` comes straight back), but the default leaks in
three more ways that no per-field marking could close:

- a **missing** field reports the whole parent object as its `input`, so a signup that forgot `name`
  returns the (valid) `password` it sent next to it;
- a body that is not an object (a JSON string, a list, a form post) is reported whole, as the `input`
  of an error whose `loc` is just `["body"]`;
- a field of the wrong type reports the value it was given, which may be an object that contains the
  secret.

The password rules say a password is never "returned in any response schema".
The response only goes back to the caller who sent it, but it also reaches whatever records
responses (a proxy log, the browser's network log, an error-reporting hook), so the rule is kept at
the one place every validation error passes through.

**One mechanism, for every field and every route.** Marking "secret" fields cannot work, because the
leak above is a *sibling's* value; and nothing needs `input` (the frontend reads `type` and `loc`
only, `frontend/src/api/requestLimits.ts`; no backend test reads it). So `input` is dropped from every
error, and the entry is rebuilt from an allow-list rather than by deleting the known-bad keys, so a key
a later pydantic release adds is dropped too:

- `type`, `loc`, `msg` are kept: the field, the rule, and the sentence the form shows. They describe the
  schema, not the value, with two exceptions this module does not try to handle: a validator that
  interpolates the value into its `ValueError`, and a parser message that quotes a character of it (a
  malformed UUID: "found `z` at 1"). Neither can reach a secret today: every validator in
  `app/schemas/` has a fixed message, and every secret-bearing input is a plain `str` bounded only by
  length (`tests/test_validation_errors.py` pins that, and the echo tests in `tests/test_local_accounts.py`
  submit distinctive values to each).
- `ctx` is kept only for the numeric bounds of a rule (`max_length: 8000`, `ge: 1`): pydantic also puts
  parser output there (the `error` text of a malformed UUID quotes the offending character; of a
  malformed JSON body, the parser's position text), and none of that is the rule.

The response is produced by FastAPI's own `request_validation_exception_handler` from the cleaned
errors, so the status and the `{"detail": [...]}` shape are exactly the default's.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

# The entry's keys that describe the schema and the failed rule. Everything else (`input`, `url`, ...)
# is dropped, including keys that do not exist yet.
KEPT_ERROR_KEYS = ("type", "loc", "msg")

# The `ctx` keys that restate a numeric bound of the field's own rule. The caller can read them from
# the OpenAPI schema; they are not derived from what was submitted.
RULE_BOUND_CTX_KEYS = frozenset({"min_length", "max_length", "gt", "ge", "lt", "le", "multiple_of"})


def _is_a_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def scrub_validation_error(error: Mapping[str, Any]) -> dict[str, Any]:
    """One pydantic error entry, reduced to the field, the rule and the sentence."""
    scrubbed = {key: error[key] for key in KEPT_ERROR_KEYS if key in error}
    ctx = error.get("ctx")
    if isinstance(ctx, Mapping):
        bounds = {k: v for k, v in ctx.items() if k in RULE_BOUND_CTX_KEYS and _is_a_number(v)}
        if bounds:
            scrubbed["ctx"] = bounds
    return scrubbed


def scrub_validation_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [scrub_validation_error(error) for error in errors]


async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """The 422 for a request FastAPI could not validate. Registered by `main.create_app`.

    The new `RequestValidationError` deliberately carries no `body=`: that is the raw submitted body,
    and nothing downstream of this handler has a use for it."""
    return await request_validation_exception_handler(request, RequestValidationError(scrub_validation_errors(exc.errors())))
