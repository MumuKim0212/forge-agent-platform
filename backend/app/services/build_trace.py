"""A build's trace: computed, bounded, stored and read back.

Three parts, all display-only -- **no decision, gate, check, routing, cost figure or
failure detail reads any of this**. `tests/test_build_trace_capture.py`
pins which files under `app/` may name `BuildTrace` / `build_traces` / this module, so a
new consumer fails a test instead of passing unseen. This module is the only one that
touches the table's content; the routes and the executor go through it.

1. The proxy's call record -- captured and bounded in L2's proxy (`llm_proxy.py`), read
   back at build end and normalised here (`normalise_calls`) into the shape stored.
2. The diff -- computed here (`compute_diff`) at publish, from the SAME two trees
   `change_set.measure_changes` compares, so a changed-file list and a diff cannot
   disagree. A measured fact whose characters are agent-chosen.
3. The sandbox log tail -- an agent claim (`bound_log`).

Capturing never fails a build: every function the executor calls here is
used inside its own `try`, and `store_trace` writes inside a SAVEPOINT so a failed insert
cannot poison the session the build's own status is written through.
"""

from __future__ import annotations

import difflib
import json
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.build import Build
from app.models.build_trace import BuildTrace
from app.schemas.build_trace import (
    BuildTraceOut,
    DiffFileOut,
    TraceBlockClaim,
    TraceCallsMeasured,
    TraceClaimsOut,
    TraceDiffOut,
    TraceLogOut,
    TraceMeasuredOut,
    TraceStepClaims,
    TraceStepMeasured,
    TraceThinkingOut,
)
from app.services.change_set import MAX_CHANGED_FILES, changed_paths

# Backend constants, not settings.
BLOCK_MAX_CHARS = 8_000  # one content field (the proxy applies it first; re-applied here)
DIFF_FILE_MAX_BYTES = 64 * 1024
DIFF_TOTAL_MAX_BYTES = 1024 * 1024
LOG_TAIL_MAX_BYTES = 256 * 1024
# Stored call record: the proxy bounds content at 2 MB and its step list at 4000 entries
# (~400 bytes each); this is the ceiling for what is accepted back from it, so a proxy
# that did not hold its own bounds cannot make a row of any size.
CALLS_STORE_MAX_BYTES = 4 * 1024 * 1024
FIELD_MAX_CHARS = 128

# A file is diffed only when it is a modest text file: `difflib` is quadratic in the
# worst case, and the diff is cut at DIFF_FILE_MAX_BYTES anyway.
DIFF_READ_MAX_BYTES = 256 * 1024
DIFF_MAX_LINES = 5_000

CALLS_CAPTURED = "captured"
CALLS_UNAVAILABLE = "unavailable"

DIFF_COMPUTED = "computed"
DIFF_NOT_REACHED = "not_reached"  # the build ended before publish
DIFF_UNAVAILABLE = "unavailable"

# `DiffFileOut.content`: why a file has, or has not, a diff text.
CONTENT_TEXT = "text"
CONTENT_BINARY = "binary"  # a NUL byte, or not UTF-8
CONTENT_TOO_LARGE = "too_large"
CONTENT_TOTAL_LIMIT = "total_limit"  # the 1 MB total was already spent

TOKEN_MARKER = "[proxy-token-redacted]"

_NO_NEWLINE = "\\ No newline at end of file\n"


# --- the diff -----------------------------------------------------------------


def compute_diff(*, input_dir: str, output_dir: str) -> dict:
    """A unified diff per changed text file, `input_dir` against `output_dir`, ordered by
    path -- the files `change_set.measure_changes` lists, no more and no fewer (up to
    `MAX_CHANGED_FILES`, with `files_total` the true count). Call it BEFORE the
    protected-path restore, on the same trees.

    Binary (a NUL byte, or not UTF-8) and oversized files are listed WITHOUT content. Each
    file's text is cut at `DIFF_FILE_MAX_BYTES` and the sum at `DIFF_TOTAL_MAX_BYTES`;
    every cut sets `truncated` on the file, and the record's `truncated` is true when any
    file was cut or the list was. Raises `OSError` if a file cannot be read: the caller
    records the diff as unavailable."""
    changes = changed_paths(input_dir=input_dir, output_dir=output_dir)
    remaining = DIFF_TOTAL_MAX_BYTES
    files: list[dict] = []
    for path, change, before, after in changes[:MAX_CHANGED_FILES]:
        entry = _file_diff(path, change, before, after, remaining)
        remaining -= len((entry["diff"] or "").encode("utf-8"))
        files.append(entry)
    return {
        "files": files,
        "files_total": len(changes),
        "truncated": len(changes) > MAX_CHANGED_FILES or any(f["truncated"] for f in files),
    }


def _file_diff(path: str, change: str, before: str | None, after: str | None, remaining: int) -> dict:
    entry = {"path": path, "change": change, "content": CONTENT_TEXT, "diff": None, "truncated": False}
    old = _read_text(before)
    new = _read_text(after)
    if old is _BINARY or new is _BINARY:
        entry["content"] = CONTENT_BINARY
        return entry
    if old is _TOO_LARGE or new is _TOO_LARGE:
        entry["content"] = CONTENT_TOO_LARGE
        return entry
    if remaining <= 0:
        entry["content"] = CONTENT_TOTAL_LIMIT
        entry["truncated"] = True
        return entry
    old_lines = _lines(old or "")
    new_lines = _lines(new or "")
    if len(old_lines) > DIFF_MAX_LINES or len(new_lines) > DIFF_MAX_LINES:
        entry["content"] = CONTENT_TOO_LARGE
        return entry
    text = "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="/dev/null" if before is None else f"a/{path}",
            tofile="/dev/null" if after is None else f"b/{path}",
        )
    )
    entry["diff"], entry["truncated"] = _cut_bytes(text, min(DIFF_FILE_MAX_BYTES, remaining))
    return entry


_BINARY = object()
_TOO_LARGE = object()


def _read_text(file_path: str | None) -> str | object | None:
    """None for no file; the text of a modest UTF-8 file; else the `_BINARY` /
    `_TOO_LARGE` sentinel (never a string a file could itself hold)."""
    if file_path is None:
        return None
    if os.path.getsize(file_path) > DIFF_READ_MAX_BYTES:
        return _TOO_LARGE
    with open(file_path, "rb") as f:
        data = f.read(DIFF_READ_MAX_BYTES + 1)
    if b"\0" in data:
        return _BINARY
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return _BINARY


def _lines(text: str) -> list[str]:
    """Lines split on `\\n` only (CRLF and a missing final newline stay visible, as in
    git). A last line with no newline carries git's own marker, so `foo` and `foo\\n`
    are different files in the diff."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1] + "\n" + _NO_NEWLINE)
    return lines


def _cut_bytes(text: str, limit: int) -> tuple[str, bool]:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    return data[:limit].decode("utf-8", errors="ignore"), True


# --- the log ------------------------------------------------------------------


def bound_log(logs: str | None, *, token: str | None = None) -> tuple[str | None, bool]:
    """`(text, truncated)`: the TAIL of the sandbox's combined output, at most
    `LOG_TAIL_MAX_BYTES`, with the per-build proxy token replaced before the cut (it is
    in the sandbox's environment, so a command that prints its env would show it). None
    when the adapter returned no log. An agent claim -- never read by any decision, and
    never part of `failure_detail`."""
    if logs is None:
        return None, False
    text = logs.replace(token, TOKEN_MARKER) if token else logs
    data = text.encode("utf-8")
    if len(data) <= LOG_TAIL_MAX_BYTES:
        return text, False
    return data[-LOG_TAIL_MAX_BYTES:].decode("utf-8", errors="ignore"), True


# --- the call record, as read back from the proxy -------------------------------


def normalise_calls(raw: object) -> dict:
    """The proxy's record, copied through an allowlist of keys and types and bounded again
    (the proxy is trusted code in L2, but a stale image or a changed shape must not become
    a row of any size or shape). Raises `ValueError` if it is not a call record at all."""
    if not isinstance(raw, dict) or not isinstance(raw.get("steps"), list):
        raise ValueError("the proxy's trace is not a call record")
    out = {
        "steps": [_normal_step(step) for step in raw["steps"]],
        "calls_total": _int(raw.get("calls_total")) or 0,
        "content_truncated": bool(raw.get("content_truncated")),
        "omitted_calls": _int(raw.get("omitted_calls")) or 0,
        "omitted_tokens": _int(raw.get("omitted_tokens")) or 0,
        "omitted_cost_usd": _float(raw.get("omitted_cost_usd")) or 0.0,
    }
    if len(json.dumps(out, ensure_ascii=False)) > CALLS_STORE_MAX_BYTES:
        raise ValueError("the proxy's trace is larger than the stored bound")
    return out


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _str(value: object, limit: int = FIELD_MAX_CHARS) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _normal_step(step: object) -> dict:
    if not isinstance(step, dict):
        raise ValueError("a step is not an object")
    content = step.get("content")
    if content not in ("recorded", "unparsed", "dropped"):
        content = "unparsed"
    thinking = [{"chars": _int(t.get("chars")) or 0} for t in step.get("thinking") or [] if isinstance(t, dict)]
    blocks = [b for b in (_normal_block(b) for b in step.get("blocks") or []) if b is not None]
    return {
        "ordinal": _int(step.get("ordinal")) or 0,
        "started_at": _str(step.get("started_at")),
        "ended_at": _str(step.get("ended_at")),
        "model": _str(step.get("model")),
        "input_tokens": _int(step.get("input_tokens")),
        "output_tokens": _int(step.get("output_tokens")),
        "cache_read_tokens": _int(step.get("cache_read_tokens")),
        "cache_write_tokens": _int(step.get("cache_write_tokens")),
        "tokens": _int(step.get("tokens")),
        "cost_usd": _float(step.get("cost_usd")),
        "stop_reason": _str(step.get("stop_reason")),
        "content": content,
        "thinking": thinking,
        "blocks": blocks,
    }


def _normal_block(block: object) -> dict | None:
    if not isinstance(block, dict):
        return None
    kind = block.get("kind")
    if kind == "other":
        return {"kind": "other", "type": _str(block.get("type"))}
    if kind not in ("assistant_text", "tool_use", "tool_result"):
        return None
    text = block.get("text")
    text = text[:BLOCK_MAX_CHARS] if isinstance(text, str) else ""
    out: dict = {
        "kind": kind,
        "text": text,
        "chars": _int(block.get("chars")) or len(text),
        "truncated": bool(block.get("truncated")) or (isinstance(block.get("text"), str) and len(block["text"]) > BLOCK_MAX_CHARS),
    }
    if kind == "tool_use":
        out["id"] = _str(block.get("id"))
        out["name"] = _str(block.get("name"))
    elif kind == "tool_result":
        out["tool_use_id"] = _str(block.get("tool_use_id"))
        is_error = block.get("is_error")
        out["is_error"] = is_error if isinstance(is_error, bool) else None
    return out


# --- storing ------------------------------------------------------------------


def store_trace(
    session: Session,
    build: Build,
    *,
    calls: dict | None,
    diff_state: str,
    diff: dict | None,
    logs: str | None,
    token: str | None = None,
) -> None:
    """Writes the build's one trace row. `calls` is the NORMALISED record, or None when
    the proxy's could not be read back. Runs inside a SAVEPOINT: if the insert fails, only
    the trace is rolled back -- the build's own pending changes and the session stay as
    they were. The caller still wraps this in its own `try`; the
    exception is for it to log, not for the build to answer to."""
    log_tail, log_truncated = bound_log(logs, token=token)
    with session.begin_nested():
        session.add(
            BuildTrace(
                build_id=build.id,
                calls_state=CALLS_CAPTURED if calls is not None else CALLS_UNAVAILABLE,
                calls=calls,
                diff_state=diff_state,
                diff=diff,
                log_tail=log_tail,
                log_truncated=log_truncated,
            )
        )


def build_ids_with_trace(db: Session, build_ids: list[str]) -> set[str]:
    """Which of these builds have a stored trace -- ids only, no content."""
    if not build_ids:
        return set()
    return set(db.scalars(select(BuildTrace.build_id).where(BuildTrace.build_id.in_(build_ids))))


def delete_traces_for_project(db: Session, project_id: str) -> int:
    """removes every trace of the project's builds (the deprovision
    action's own transaction; the caller commits). The builds stay."""
    traces = db.scalars(
        select(BuildTrace).join(Build, Build.id == BuildTrace.build_id).where(Build.project_id == project_id)
    ).all()
    for trace in traces:
        db.delete(trace)
    return len(traces)


# --- reading ------------------------------------------------------------------


def load_trace_view(db: Session, build_id: str) -> BuildTraceOut | None:
    """The API shape of a build's trace, measured fields and claims apart, or None when
    the build has none. Call it only AFTER the access check (`roles.may_read_build_trace`):
    this is the one place trace content is loaded for a reader."""
    row = db.scalar(select(BuildTrace).where(BuildTrace.build_id == build_id))
    if row is None:
        return None
    return _view(row)


def _view(row: BuildTrace) -> BuildTraceOut:
    calls = row.calls or {}
    steps = calls.get("steps") or []
    diff = row.diff or {}
    return BuildTraceOut(
        build_id=row.build_id,
        captured_at=row.created_at,
        measured=TraceMeasuredOut(
            calls=TraceCallsMeasured(
                state=row.calls_state,
                steps=[
                    TraceStepMeasured(
                        **{k: step.get(k) for k in _MEASURED_STEP_KEYS},
                        thinking=[TraceThinkingOut(chars=t["chars"]) for t in step.get("thinking") or []],
                    )
                    for step in steps
                ],
                calls_total=calls.get("calls_total", 0),
                content_truncated=calls.get("content_truncated", False),
                omitted_calls=calls.get("omitted_calls", 0),
                omitted_tokens=calls.get("omitted_tokens", 0),
                omitted_cost_usd=calls.get("omitted_cost_usd", 0.0),
            ),
            diff=TraceDiffOut(
                state=row.diff_state,
                files=[DiffFileOut(**f) for f in diff.get("files") or []],
                files_total=diff.get("files_total", 0),
                truncated=diff.get("truncated", False),
            ),
        ),
        claims=TraceClaimsOut(
            calls=[
                TraceStepClaims(
                    ordinal=step["ordinal"], blocks=[TraceBlockClaim(**b) for b in step.get("blocks") or []]
                )
                for step in steps
            ],
            log=TraceLogOut(text=row.log_tail, truncated=row.log_truncated),
        ),
    )


_MEASURED_STEP_KEYS = (
    "ordinal",
    "started_at",
    "ended_at",
    "model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "tokens",
    "cost_usd",
    "stop_reason",
    "content",
)
