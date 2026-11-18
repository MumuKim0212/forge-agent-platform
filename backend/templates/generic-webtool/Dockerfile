# Template-owned build manifest (protected path)
# for the generic web-tool template. Image
# assembly always builds from this pinned file, never from an
# agent-modified copy (agent changes to this path are dropped at harvest
# and reported as drift; image_assembly.py additionally re-copies it over
# the workspace before every build).
#
# the seed app (app/app.py) is a real FastAPI service exposing
# GET /healthz, so the built image is health-gate-able and
# previewable as a real HTTP service.
#
# ADR 0023 §Schema migrations: the template's data contract lands
# here, in this CMD, and nowhere else. `alembic upgrade head` runs before
# uvicorn ever serves a request -- this is the one file the agent
# provably cannot change (image_assembly.py re-pins it before every
# build), so "migrations run before serving" is guaranteed by the same
# mechanism that already guarantees the app can't smuggle in a different
# entrypoint. `exec` in the final step replaces the shell with uvicorn so
# it keeps PID 1 and still receives/forwards signals cleanly (a plain
# `uvicorn ...` without `exec`, run as the second half of `&&` inside
# `sh -c`, would leave the shell as PID 1 instead).
FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.32" \
    "sqlalchemy>=2.0" \
    "alembic>=1.13" \
    "psycopg[binary]>=3.1" \
    "redis>=5.0"

COPY app/ /app/

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app:app --host 0.0.0.0 --port 8000"]
