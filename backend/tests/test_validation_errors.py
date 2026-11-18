"""a validation error names the field and the failed rule and never the value submitted.

`app/services/validation_errors.py` is the one handler every 422 passes through. This file tests it
three ways:

- the scrubbing itself, on real pydantic errors (with a check that the raw errors really do carry what
  is scrubbed, so a scrub that does nothing cannot pass);
- the shape a client still gets on routes that carry no secret, through the shipped `app`: field, rule
  and bound intact, `input` gone, so the frontend's reading of `type` and `loc` keeps working;
- the registry of every route input that looks like a secret, so a new one cannot arrive without a
  verdict. The routes that take a password are driven through every failure shape in
  `tests/test_local_accounts.py`; this file pins that they are all of them.
"""

import importlib
import pkgutil
import re
import typing
from uuid import UUID

import pytest
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, ValidationError

import app.routers as routers_package
from app.models.user import User
from app.services.roles import Role
from app.services.validation_errors import (
    KEPT_ERROR_KEYS,
    RULE_BOUND_CTX_KEYS,
    scrub_validation_error,
    scrub_validation_errors,
)
from tests.test_local_accounts import ECHO_TESTED_ROUTES

# -- the scrubbing -----------------------------------------------------------


class _Probe(BaseModel):
    password: str = Field(min_length=8)
    ident: UUID
    count: int = Field(ge=1)


def _raw_errors():
    with pytest.raises(ValidationError) as caught:
        _Probe.model_validate({"password": "Zq7!vK", "ident": "zz-not-a-uuid-7c1", "count": 0})
    return caught.value.errors()


def test_the_raw_pydantic_errors_carry_what_the_handler_exists_to_remove():
    """The premise, observed: without it the scrub tests below would pass against a scrub that did nothing."""
    by_field = {error["loc"][0]: error for error in _raw_errors()}

    assert by_field["password"]["input"] == "Zq7!vK"
    assert by_field["ident"]["input"] == "zz-not-a-uuid-7c1"
    assert "found `z`" in by_field["ident"]["ctx"]["error"]  # parser output quotes the offending character
    assert "url" in by_field["password"]


def test_scrubbing_keeps_the_field_the_rule_and_the_rules_own_bound():
    scrubbed = scrub_validation_errors(_raw_errors())

    assert scrubbed[0] == {
        "type": "string_too_short",
        "loc": ("password",),
        "msg": "String should have at least 8 characters",
        "ctx": {"min_length": 8},
    }
    assert scrubbed[2] == {
        "type": "greater_than_equal",
        "loc": ("count",),
        "msg": "Input should be greater than or equal to 1",
        "ctx": {"ge": 1},
    }


def test_scrubbing_drops_the_input_the_url_and_parser_output_in_ctx():
    for error in scrub_validation_errors(_raw_errors()):
        assert "input" not in error and "url" not in error
    uuid_error = scrub_validation_errors(_raw_errors())[1]
    assert uuid_error["type"] == "uuid_parsing"
    assert "ctx" not in uuid_error  # its only ctx entry was the parser's `error` text


def test_scrubbing_is_an_allow_list_so_a_key_added_later_is_dropped_too():
    entry = {
        "type": "missing",
        "loc": ["body", "name"],
        "msg": "Field required",
        "input": {"password": "a-secret-value"},
        "url": "https://errors.pydantic.dev/x",
        "a_key_pydantic_adds_in_some_future_release": "a-secret-value",
        "ctx": {"max_length": 100, "expected": "'a-secret-value'", "error": "a-secret-value", "ge": "2020-01-01"},
    }

    assert scrub_validation_error(entry) == {
        "type": "missing",
        "loc": ["body", "name"],
        "msg": "Field required",
        "ctx": {"max_length": 100},
    }


def test_the_two_allow_lists_are_what_the_docs_say():
    assert KEPT_ERROR_KEYS == ("type", "loc", "msg")
    assert RULE_BOUND_CTX_KEYS == frozenset({"min_length", "max_length", "gt", "ge", "lt", "le", "multiple_of"})


# -- the shape a client still gets, through the shipped app ------------------


def _as(user_id):
    return {"X-Forge-User-Id": user_id}


def test_a_query_bound_error_keeps_its_field_rule_and_bound_and_loses_the_input(client, db_session):
    admin = db_session.query(User).filter_by(role=Role.ADMIN).first()

    response = client.get("/api/usage/dashboard?limit=51", headers=_as(admin.id))

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "less_than_equal",
                "loc": ["query", "limit"],
                "msg": "Input should be less than or equal to 50",
                "ctx": {"le": 50},
            }
        ]
    }


def test_a_value_that_does_not_parse_is_not_echoed_from_a_query(client, db_session):
    admin = db_session.query(User).filter_by(role=Role.ADMIN).first()

    response = client.get("/api/usage/dashboard?limit=zz-4f9c-nope", headers=_as(admin.id))

    assert response.status_code == 422
    assert [(e["type"], e["loc"]) for e in response.json()["detail"]] == [("int_parsing", ["query", "limit"])]
    assert "zz-4f9c-nope" not in response.text


def test_an_over_long_field_keeps_the_entry_the_frontend_recognises(client, seeded_ids):
    """`frontend/src/api/requestLimits.ts` reads `type` and `loc` and nothing else (pinned there and in
    test_request_length_cap.py); this is the whole entry, so the `ctx` bound survives too."""
    response = client.post(
        "/api/projects",
        headers=_as(seeded_ids["user_id"]),
        json={
            "name": "n" * 101,
            "template_id": seeded_ids["template_id"],
            "team_id": seeded_ids["team_id"],
            "nl_request": "a request that is fine",
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": [
            {
                "type": "string_too_long",
                "loc": ["body", "name"],
                "msg": "String should have at most 100 characters",
                "ctx": {"max_length": 100},
            }
        ]
    }


def test_a_missing_field_does_not_bring_its_siblings_back(client, seeded_ids):
    """Pydantic reports the whole parent object as a missing field's `input`: the request text, here,
    and a password on the routes that take one."""
    response = client.post(
        "/api/projects",
        headers=_as(seeded_ids["user_id"]),
        json={"name": "fine", "team_id": seeded_ids["team_id"], "nl_request": "a-distinctive-request-text-9f2"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": [{"type": "missing", "loc": ["body", "template_id"], "msg": "Field required"}]
    }
    assert "a-distinctive-request-text-9f2" not in response.text


# -- every route input that looks like a secret has a verdict ------------------

# A name that suggests a credential, a token, or a value an attacker would like to read back.
_SECRET_LOOKING = re.compile(r"pass|secret|token|credential|key|dsn|code|state|auth|cookie|session|signature|nonce|hash", re.I)

# Where the verdicts are:
# - "echo-tested": a password in a request body. Driven through every failure shape in
#   tests/test_local_accounts.py (checked against ECHO_TESTED_ROUTES below);
# - "plain-str": a query value read as an unconstrained optional `str`, so no rule can fail on it and no
#   422 can quote it (checked below). `/auth/callback` reads the OIDC `code` and `state`, the preview
#   proxy reads the signed `token`.
PINNED_SECRET_LOOKING_INPUTS = {
    ("auth", "/auth/callback", "query", "code"): "plain-str",
    ("auth", "/auth/callback", "query", "state"): "plain-str",
    ("auth_local", "/auth/signup", "body", "password"): "echo-tested",
    ("auth_local", "/auth/login", "body", "password"): "echo-tested",
    ("auth_local", "/auth/password", "body", "current_password"): "echo-tested",
    ("auth_local", "/auth/password", "body", "new_password"): "echo-tested",
    ("auth_local", "/admin/users/{user_id}/password", "body", "password"): "echo-tested",
    ("preview_proxy", "/projects/{project_id}/preview/{build_id}", "query", "token"): "plain-str",
    ("preview_proxy", "/projects/{project_id}/preview/{build_id}/{path:path}", "query", "token"): "plain-str",
}


def _model_fields(annotation, seen=()):
    """(name, annotation, metadata) of every field of every pydantic model the annotation names, nested ones included."""
    for arg in typing.get_args(annotation) or (annotation,):
        if isinstance(arg, type) and issubclass(arg, BaseModel) and arg not in seen:
            for name, info in arg.model_fields.items():
                yield name, info.annotation, info.metadata
                yield from _model_fields(info.annotation, (*seen, arg))
        elif typing.get_args(arg):
            yield from _model_fields(arg, seen)


def _inputs(dependant):
    for kind in ("query", "header", "cookie", "path"):
        for field in getattr(dependant, f"{kind}_params"):
            name = field.alias if kind == "header" else field.name
            yield kind, name, field.field_info.annotation, field.field_info.metadata
    for field in dependant.body_params:
        for name, annotation, metadata in _model_fields(field.field_info.annotation):
            yield "body", name, annotation, metadata
    for sub in dependant.dependencies:
        yield from _inputs(sub)


def _secret_looking_inputs():
    """Every router module in `app/routers/` (conditionally registered ones included), every route in it:
    {(module, path, where, name): (annotation, metadata)} for the inputs whose name looks like a secret.
    Read from the routers themselves, not from the app a particular mode builds."""
    found = {}
    for module_info in pkgutil.iter_modules(routers_package.__path__):
        router = getattr(importlib.import_module(f"app.routers.{module_info.name}"), "router", None)
        for route in getattr(router, "routes", ()):
            if isinstance(route, APIRoute):
                for where, name, annotation, metadata in _inputs(route.dependant):
                    if _SECRET_LOOKING.search(name):
                        found[(module_info.name, route.path, where, name)] = (annotation, metadata)
    return found


def test_every_route_input_that_looks_like_a_secret_has_a_verdict():
    found = set(_secret_looking_inputs())

    assert found == set(PINNED_SECRET_LOOKING_INPUTS), (
        "a route input with a secret-looking name appeared or went away. A password in a body belongs in "
        "ECHO_TESTED_ROUTES (tests/test_local_accounts.py) and here as 'echo-tested'; a plain query "
        "value as 'plain-str'. Anything else needs its own test of what a 422 on it echoes. "
        f"new: {sorted(found - set(PINNED_SECRET_LOOKING_INPUTS))}; gone: {sorted(set(PINNED_SECRET_LOOKING_INPUTS) - found)}"
    )


def test_each_echo_tested_input_is_a_plain_str_and_is_driven_by_the_echo_tests():
    found = _secret_looking_inputs()
    covered = {
        (path, name): route  # the table spells the full path, `/api` included
        for route, (path, _valid, secret_fields, _set) in ECHO_TESTED_ROUTES.items()
        for name in secret_fields
    }
    echo_tested = {key for key, verdict in PINNED_SECRET_LOOKING_INPUTS.items() if verdict == "echo-tested"}

    for module, path, where, name in echo_tested:
        assert where == "body"
        assert ("/api" + path, name) in covered, f"{path} {name} is not in ECHO_TESTED_ROUTES"
        annotation, _metadata = found[(module, path, where, name)]
        # Only length rules apply to a `str`, so no parser message can quote a character of the value.
        assert annotation is str, f"{path} {name} is {annotation}; its parse errors need their own look"
    assert set(covered) == {("/api" + path, name) for _module, path, _where, name in echo_tested}, (
        "ECHO_TESTED_ROUTES drives a route input this registry does not list as echo-tested"
    )


def test_each_plain_str_input_is_an_unconstrained_optional_str():
    found = _secret_looking_inputs()
    plain = [key for key, verdict in PINNED_SECRET_LOOKING_INPUTS.items() if verdict == "plain-str"]

    for key in plain:
        annotation, metadata = found[key]
        assert key[2] == "query"
        assert annotation == (str | None), f"{key} is {annotation}, so a value can fail on it"
        assert list(metadata) == [], f"{key} carries rules {metadata}, so a value can fail on it"
