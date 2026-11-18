# LLM egress proxy image (ADR 0014).
#
# Runs `python -m app.proxy_server` — a small FastAPI/uvicorn app that holds
# the real Anthropic API key (injected via env at container start, never
# baked into this image) and forwards requests from sandboxed builds after
# swapping their ephemeral proxy token for the real key.
#
# Built from the backend source tree so it always matches the platform
# code; the real key never appears in this Dockerfile or any layer.
#
# this container is L2's
# but L3 can reach it, and it holds the real key, so
#   - its dependencies are pinned by `backend/constraints.txt`, the same
#     file CI and the dev scripts install with. The base image is the Python minor
#     that file was resolved on (`python:3.14-slim`, see the file's header): a pin
#     set is only a promise on the interpreter it was generated for. Build from
#     `backend/` so the file is in the context:
#         docker build -t forge-llm-proxy:latest -f docker/llm-proxy/Dockerfile .
#     `-c` constrains, it does not add: a package that is installed here but not
#     pinned is an unpinned install, and tests/test_llm_proxy_dockerfile.py fails;
#   - it runs as a non-root user (the container is also started with `user=` and
#     with its capabilities dropped, memory and process caps and no-new-privileges,
#     `services/proxy_container.py`).
FROM python:3.14-slim

WORKDIR /app

COPY constraints.txt /app/constraints.txt
RUN pip install --no-cache-dir -c /app/constraints.txt fastapi 'uvicorn[standard]' httpx pydantic

COPY app /app/app

# what this image can do, which the worker reads with one `image inspect` (at its start, and
# before every build's proxy container is created; `services/proxy_container.py`
# REQUIRED_PROXY_CAPABILITIES). `drain`: it serves `POST /_forge_admin/tokens/{token}/drain`.
# An image built before this line has no label and is refused by name, not found out per build after
# a paid agent run. Add a name here when the proxy gains something the worker depends on;
# tests/test_proxy_image_capabilities.py pins this value.
LABEL forge.proxy.capabilities="drain"

# Nothing in /app is written at run time; no bytecode is written either (the
# directory is root-owned and the user below could not write it).
ENV PYTHONDONTWRITEBYTECODE=1

# nobody:nogroup. Numeric, so it needs no passwd entry and means the same in any base.
USER 65534:65534

# The sandbox-facing listener. The admin listener (8802) is bound to the
# container's own loopback and is deliberately not exposed.
EXPOSE 8801

ENTRYPOINT ["python", "-m", "app.proxy_server"]
