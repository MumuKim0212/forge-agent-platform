"""The platform DB URL, as the installed driver wants it.

Only psycopg v3 is installed. A bare `postgresql://` resolves to psycopg2 on SQLAlchemy 2.0.x (which
`pyproject.toml`'s `>=2.0` bound allows) and fails at import; the pinned 2.1.x happens to default to
psycopg v3, so there it works either way. The old `postgres://` spelling is refused by every
SQLAlchemy 2 (`NoSuchModuleError`). Naming the driver makes the result independent of the
SQLAlchemy version. `services/service_data.py` already forces the driver for service URLs; this does
the same for the platform's own `DATABASE_URL`.

Imports nothing from `app.config`, so `app.preflight` can use it before settings have loaded."""

_DRIVER_PREFIX = "postgresql+psycopg://"
_BARE_PREFIXES = ("postgresql://", "postgres://")


def normalise_database_url(url: str) -> str:
    """`postgresql://...` and `postgres://...` become `postgresql+psycopg://...`. A URL that names a
    driver already (`postgresql+asyncpg://`, `postgresql+psycopg://`), SQLite and anything else is
    returned unchanged. A string operation on the scheme only: the rest, a percent-encoded password
    included, is byte for byte what the operator wrote."""
    for prefix in _BARE_PREFIXES:
        if url.startswith(prefix):
            return _DRIVER_PREFIX + url[len(prefix) :]
    return url
