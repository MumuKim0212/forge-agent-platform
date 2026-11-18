# Seed application. Not protected --
# this is exactly the kind of file the agent is expected to replace/extend
# inside the template shape (template = structure, not feature).
#
# ADR 0023 §Data services / §Schema migrations: demonstrates the
# template's data contract end to end -- one table (created by the
# shipped initial migration, migrations/versions/), one endpoint that
# writes a row and reads it straight back, and one Redis round-trip.
# Deliberately not a product: resist adding a domain model here.
#
# GET / is this service's SCREEN -- the one thing a requester
# ever sees, in a browser. It serves static/index.html, a self-contained
# starter page (inline CSS and script, relative URLs, no external
# resource) that lists the `items` below and adds one through `fetch`.
# It is the page to REPLACE with the screen the request asked for, not
# a placeholder to leave in place.
#
# GET /healthz stays cheap and never touches the database or Redis:
# it is what `wait_healthy` gates a deployment on, and by
# the time this app is serving a request at all, `alembic upgrade head`
# (this template's Dockerfile CMD) has already run against DATABASE_URL --
# database reachability is proven by that having succeeded, not by this
# endpoint probing it again.

from datetime import datetime, timezone

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from cache import redis_client
from db import SessionLocal

app = FastAPI(title="forge-generic-webtool")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    """The service's screen. Read per request, so an edit to the
    page shows without a restart."""
    return HTMLResponse((Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8"))


@app.post("/items")
def create_and_read_item(value: str = "hello from forge") -> dict:
    """Writes a row into the table the shipped initial migration creates,
    then reads it straight back in the same request -- the template's one
    endpoint that reads and writes it. Takes `value` as a query
    parameter rather than a JSON body: this template's health/data
    contract is exercised over `docker exec` (`DeployManager._exec_http`),
    which sends no request body, so every endpoint this seed ships stays
    reachable without one."""
    created_at = datetime.now(timezone.utc)
    with SessionLocal() as session:
        inserted = session.execute(
            text("INSERT INTO items (value, created_at) VALUES (:value, :created_at) RETURNING id"),
            {"value": value, "created_at": created_at},
        )
        new_id = inserted.scalar_one()
        session.commit()

        row = session.execute(
            text("SELECT id, value, created_at FROM items WHERE id = :id"),
            {"id": new_id},
        ).one()
    return {"id": row.id, "value": row.value, "created_at": row.created_at.isoformat()}


@app.get("/items")
def list_items() -> list[dict]:
    """Read-only listing over the same table -- lets a caller observe that
    the table exists (and what is in it) without also writing to it."""
    with SessionLocal() as session:
        rows = session.execute(text("SELECT id, value, created_at FROM items ORDER BY id")).all()
    return [{"id": r.id, "value": r.value, "created_at": r.created_at.isoformat()} for r in rows]


@app.post("/cache-roundtrip")
def cache_roundtrip() -> dict:
    """Writes a key to Redis and reads it straight back -- the template's
    one Redis round-trip."""
    key = "forge-generic-webtool:roundtrip"
    value = datetime.now(timezone.utc).isoformat()
    redis_client.set(key, value)
    read_back = redis_client.get(key)
    return {"key": key, "wrote": value, "read": read_back, "roundtrip_ok": read_back == value}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
