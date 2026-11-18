"""The agent engine for a build (ADR 0035 §Decision 1 and 5).

`resolve_engine` is the **only** place that decides which engine a build runs on.
The worker calls it when it claims a build, never at startup, so a change of
selection applies to the next claim and a running build is never switched. The
engine-neutral opt-in (`any_engine_available`) lives here too, so the API's
initial build status and the worker's executor choice cannot disagree with it.

The selection is DB state set by an admin (`models/engine_selection.py`), read
here on EVERY call and cached nowhere, so a change applies to the next claim. Which wins
when both exist: **the admin's row wins over `AGENT_ENGINE_DEFAULT`**, which is only the
default for a deployment where nobody has chosen yet (no row). With neither, the only
available engine, else `anthropic`.

An engine is *available* when its real key is configured. There is no fallback:
a build whose selected engine is not available fails with a platform-authored
detail naming it, because running it on the other engine would change the vendor
and the cost without anyone choosing that (ADR 0035 §Decision 5).

The set is closed: `anthropic` and `openai`. A third engine is an ADR, not
configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import Settings
from app.models.base import _utcnow
from app.models.engine_selection import SINGLETON_ID, EngineSelection
from app.models.user import User
from app.services.audit import write_audit_log

ANTHROPIC = "anthropic"
OPENAI = "openai"
ENGINES: tuple[str, ...] = (ANTHROPIC, OPENAI)


@dataclass(frozen=True)
class EngineChoice:
    """What the executor needs to run one build on one engine: the model, the pinned
    sandbox image, and the proxy's dialect, key and upstream. How the sandbox is wired
    to the engine (runner script, environment variable names) is the adapter's, not
    here. The real key is host-side only and never part of the repr."""

    engine: str
    # The model the runner is told to use; None for an engine that runs its own
    # default (the Claude runner passes none). The trace's per-call model is the
    # authoritative wire value; this is the configured one.
    model: str | None
    image: str
    dialect: str
    api_key: str = field(repr=False)  # "" only from `engine_config` with no key configured
    upstream_base_url: str


@dataclass(frozen=True)
class Unavailable:
    """The selected engine cannot run a build now. `detail` is platform-authored
    (it becomes the build's failure detail); it names the engine and why."""

    engine: str
    detail: str


def _key_for(engine: str, settings: Settings) -> str | None:
    if engine not in ENGINES:  # a corrupt stored value is never "available" (and never openai by elimination)
        return None
    key = settings.anthropic_api_key if engine == ANTHROPIC else settings.openai_api_key
    return key if key and key.strip() else None


def available_engines(settings: Settings) -> tuple[str, ...]:
    """The engines whose real key is configured, in `ENGINES` order."""
    return tuple(e for e in ENGINES if _key_for(e, settings))


def any_engine_available(settings: Settings) -> bool:
    return bool(available_engines(settings))


def sandbox_executor_active(settings: Settings) -> bool:
    """The engine-neutral opt-in (ADR 0035 §Decision 5): the sandbox executor runs,
    and the API creates QUEUED builds, when `USE_SANDBOX_EXECUTOR` is set and at
    least one engine is available. A key alone, or the flag alone, changes nothing."""
    return bool(settings.use_sandbox_executor) and any_engine_available(settings)


def _selection_row(db: Session) -> EngineSelection | None:
    return db.get(EngineSelection, SINGLETON_ID)


def _default_engine(settings: Settings) -> str:
    """The engine when nobody has chosen: `AGENT_ENGINE_DEFAULT` if set, else the only
    available engine, else `anthropic` (both available, or neither)."""
    if settings.agent_engine_default:
        return settings.agent_engine_default
    available = available_engines(settings)
    if len(available) == 1:
        return available[0]
    return ANTHROPIC


def _selected_engine(db: Session, settings: Settings) -> str:
    """Which engine is selected: the admin's row if there is one (read per call, never
    cached), else the environment/only-available default."""
    row = _selection_row(db)
    if row is not None:
        return row.engine
    return _default_engine(settings)


def engine_config(engine: str, settings: Settings) -> EngineChoice:
    """`engine`'s configuration as it stands in `settings`, WITHOUT any availability
    decision (`api_key` is "" when none is configured). The executor uses this to turn
    the engine a build already carries (`Build.engine`, written at claim) into what it
    runs; it decides nothing. An engine outside the closed set raises: a programming
    error, not a state a build can be in."""
    if engine not in ENGINES:
        raise ValueError(f"unknown engine {str(engine)[:32]!r}")
    key = _key_for(engine, settings) or ""
    if engine == ANTHROPIC:
        return EngineChoice(
            engine=ANTHROPIC,
            model=None,
            image=settings.agent_sandbox_image,
            dialect="anthropic",
            api_key=key,
            upstream_base_url=settings.anthropic_upstream_base_url,
        )
    return EngineChoice(
        engine=OPENAI,
        model=settings.openai_model,
        image=settings.openai_agent_sandbox_image,
        dialect="openai",
        api_key=key,
        upstream_base_url=settings.openai_upstream_base_url,
    )


def resolve_engine(db: Session, settings: Settings) -> EngineChoice | Unavailable:
    """The engine this build runs on: the selected one if its key is configured,
    else `Unavailable` naming it. Never the other engine."""
    engine = _selected_engine(db, settings)
    if _key_for(engine, settings) is None:
        if engine not in ENGINES:
            shown = str(engine)[:32]
            return Unavailable(
                engine=shown,
                detail=(
                    f"the stored engine selection {shown!r} is not one of the known engines; "
                    "the build did not start and was not run on another engine"
                ),
            )
        env_name = "ANTHROPIC_API_KEY" if engine == ANTHROPIC else "OPENAI_API_KEY"
        return Unavailable(
            engine=engine,
            detail=(
                f"the {engine} engine is selected for builds but its API key is not configured "
                f"({env_name}); the build did not start and was not run on another engine"
            ),
        )
    return engine_config(engine, settings)


# ---------------------------------------------------------------------------
# The admin's view and write. The router maps these errors to 422.
# ---------------------------------------------------------------------------


class EngineSelectionError(ValueError):
    """The requested selection is refused. A `ValueError` with a message safe to show."""


@dataclass(frozen=True)
class EngineStatus:
    """What the admin console shows for one engine. `available` is the ONLY fact derived
    from the key: the key, and
    whether it is set beyond availability itself, are never reported."""

    engine: str
    available: bool
    model: str | None
    image: str
    selected: bool


@dataclass(frozen=True)
class EngineOverview:
    engines: tuple[EngineStatus, ...]
    selected: str
    # "stored": an admin's stored selection. "default": none stored, the environment/only-available rule.
    source: str
    set_by_id: str | None
    set_by_name: str | None
    set_at: datetime | None


def engine_overview(db: Session, settings: Settings) -> EngineOverview:
    row = _selection_row(db)
    selected = _selected_engine(db, settings)
    statuses = []
    for engine in ENGINES:
        cfg = engine_config(engine, settings)
        statuses.append(
            EngineStatus(
                engine=engine,
                available=_key_for(engine, settings) is not None,
                model=cfg.model,
                image=cfg.image,
                selected=engine == selected,
            )
        )
    setter = db.get(User, row.set_by_id) if row is not None else None
    return EngineOverview(
        engines=tuple(statuses),
        selected=selected,
        source="stored" if row is not None else "default",
        set_by_id=row.set_by_id if row is not None else None,
        set_by_name=setter.name if setter is not None else None,
        set_at=row.set_at if row is not None else None,
    )


def set_engine_selection(db: Session, settings: Settings, *, actor_id: str, engine: str) -> str:
    """Select `engine` for builds claimed from now on, and return the engine that was
    effective before. Refuses (`EngineSelectionError`) an engine outside the closed set or
    one that is not available: a selection that could not run would only make every later
    build fail. Audit-logged with actor, old and new (`platform.engine_changed`); does not
    commit, the caller owns the transaction (same convention as `services/usage.py`)."""
    if engine not in ENGINES:
        raise EngineSelectionError(f"unknown engine; choose one of: {', '.join(ENGINES)}")
    if _key_for(engine, settings) is None:
        raise EngineSelectionError(
            f"the {engine} engine is not available: its API key is not configured, so it cannot be selected"
        )

    row = _selection_row(db)
    old_engine = row.engine if row is not None else _default_engine(settings)
    if row is None:
        db.add(EngineSelection(id=SINGLETON_ID, engine=engine, set_by_id=actor_id))
    else:
        row.engine = engine
        row.set_by_id = actor_id
        row.set_at = _utcnow()
    write_audit_log(
        db,
        actor_id=actor_id,
        action="platform.engine_changed",
        target="platform:agent_engine",
        metadata={"old_engine": old_engine, "new_engine": engine, "old_was_default": row is None},
    )
    return old_engine
