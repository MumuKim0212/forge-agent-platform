"""The closed set of LLM proxy dialects (ADR 0035 §Decision 1 and 3), by name.

One place that says which dialects exist, which environment variable carries each
one's real key, and which is the default. The proxy container's entry point
(`app/proxy_server.py`) reads it to pick a dialect from `FORGE_PROXY_DIALECT`, and
`proxy_container.py` reads it to start a container with one dialect and that
dialect's one key. A third dialect is an ADR, not configuration.

The key variable is per dialect, not neutral: a container started for `openai`
reads only `FORGE_PROXY_OPENAI_API_KEY`, so a launcher that put the other vendor's
key in the environment by mistake has it ignored rather than forwarded to the
wrong vendor, and a launcher that forgot the right one fails at startup. A proxy
never holds both keys, because the launcher only ever sets one.
"""

from __future__ import annotations

from app.services.llm_proxy import ANTHROPIC_DIALECT, Dialect
from app.services.llm_proxy_openai import OPENAI_DIALECT

DEFAULT_DIALECT = "anthropic"

DIALECTS: dict[str, Dialect] = {
    ANTHROPIC_DIALECT.name: ANTHROPIC_DIALECT,
    OPENAI_DIALECT.name: OPENAI_DIALECT,
}


class UnknownDialectError(ValueError):
    """A dialect name outside the closed set."""


def get_dialect(name: str | None) -> Dialect:
    """The dialect for `name` (None or empty means the default). Anything else not
    in `DIALECTS` raises `UnknownDialectError`; it is never mapped to a fallback."""
    resolved = name or DEFAULT_DIALECT
    try:
        return DIALECTS[resolved]
    except KeyError:
        known = ", ".join(sorted(DIALECTS))
        raise UnknownDialectError(f"unknown proxy dialect {resolved[:32]!r} (known: {known})") from None
