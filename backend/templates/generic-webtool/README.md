# generic-webtool template skeleton

The "one generic web-tool template" shape: FastAPI + PostgreSQL +
Redis, with its own data services (ADR 0023) provisioned by the
platform at first deploy. `create_repo` (`app/services/publish.py`)
seeds every new project repo from this directory.

## The data contract (ADR 0023)

The platform issues `DATABASE_URL` and `REDIS_URL` as part of the service
env; this template
consumes them by name only, picks its own Postgres driver (psycopg v3,
`app/db.py`), and runs `alembic upgrade head` before `uvicorn` ever serves
a request (the `Dockerfile`'s `CMD`). Migrations are forward-only (ADR
0023 §Schema migrations): the shipped initial migration
(`app/migrations/versions/`) only creates a table, and this template's
own `alembic revision` scaffold (`app/migrations/script.py.mako`) raises
in `downgrade()` rather than shipping a no-op that looks safe to run.

`GET /healthz` stays cheap and never touches the database or Redis --
database reachability is already proven by the time it answers, because
migrations ran first.

## The screen

`GET /` is the service's screen, because the person who asked for it only ever
uses a browser. `app/static/index.html` is a self-contained starter page (inline
CSS and script, relative URLs, no external resource) that lists `GET /items` and
adds one through `fetch`; `app/app.py` serves it. A generated service replaces it
with the page its request needs. The policy gate scans `.html`, `.htm` and `.css`
like source files. Writes go through `fetch` with query parameters or a JSON body,
never `Form(...)`: the image has no `python-multipart`.

## Protected paths (agent-untouchable)

Two sets, both declared by the platform (`Settings`, `.env.example`), never by
the agent. An entry ending in `/` is a prefix (every file under it, including
new ones); any other entry is one exact file.

**Template-owned manifests, every build type** (`PROTECTED_PATHS`):

- `Dockerfile`
- `compose.yaml`

Whatever the agent did, the published tree ends with the pinned copies from
this directory. The agent's change is reported as *drift* only when it changed
the file relative to the build's own input (the staged directory: this
template's `app/` for a create build, the confirmed ref for a modify build). A
create build whose agent never touched the Dockerfile reports no drift, even
though its input never contained one.

**Schema paths, modify-type builds only** (`PROTECTED_SCHEMA_PATHS`, ADR 0009
§Amendment 2026-10-05):

- `app/migrations/`
- `app/alembic.ini`

On a modify build, any file under these that the agent adds, edits or deletes
refuses the build (`AGENT_ERROR`) and nothing is published. Create builds are
unaffected and add forward-only migrations freely (ADR 0023).

Everything else (`app/`) is normal agent-writable workspace content, including
`app/db.py` and `app/cache.py`. Only `app/` is staged into a create build's
sandbox workspace (`sandbox_build_executor.py`), so anything the agent must be
able to see, including this template's own alembic configuration, has no other
place to live.
