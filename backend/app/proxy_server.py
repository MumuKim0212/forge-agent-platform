"""Entrypoint for running the LLM egress proxy as its own process/container
(ADR 0014): `python -m app.proxy_server`.

Reads its configuration from environment variables set by
`services/proxy_container.py` when it starts the proxy container —
never from the platform `.env` inside the proxy container image, so the
real key only ever exists in this one process's environment, injected at
container-start time, never baked into the image.

One container runs ONE dialect (ADR 0035 §Decision 3): `FORGE_PROXY_DIALECT`
(default `anthropic`) selects it, and only that dialect's key variable is read.
An unknown dialect refuses to start.

Two listeners over one token registry (resource and
time limits): the sandbox-facing one (`0.0.0.0:FORGE_PROXY_PORT`) serves the proxy
routes and NO admin route; the admin one (`127.0.0.1:FORGE_PROXY_ADMIN_PORT`) serves
`/_forge_admin/*` and is bound to the container's own loopback, which only a process
inside the container can reach -- the worker's `docker exec`. A request from the
sandbox therefore never meets the admin routes: not refused by a check that could
be wrong, but not served.
"""

import os
import threading
from collections.abc import Mapping

import uvicorn
from fastapi import FastAPI

from app.services.llm_proxy import TokenRegistry, create_app
from app.services.proxy_dialects import UnknownDialectError, get_dialect


def build_app(
    environ: Mapping[str, str], *, serve: str = "all", registry: TokenRegistry | None = None
) -> FastAPI:
    """The proxy app for the dialect `environ` names. Raises `UnknownDialectError`
    for a dialect outside the closed set and `KeyError` for a missing key or admin
    secret, so a misconfigured container exits at startup instead of serving.

    `serve` is `llm_proxy.create_app`'s (`"all"` by default, `"proxy"` or `"control"`
    for the two halves `main` runs); a `registry` shared between the two halves is
    what makes a token issued on the admin listener usable on the proxy one."""
    dialect = get_dialect(environ.get("FORGE_PROXY_DIALECT"))
    real_api_key = environ[dialect.key_env_var]
    admin_secret = environ["FORGE_PROXY_ADMIN_SECRET"]
    upstream_base_url = environ.get("FORGE_PROXY_UPSTREAM_BASE_URL") or None

    return create_app(
        registry if registry is not None else TokenRegistry(),
        real_api_key=real_api_key,
        admin_secret=admin_secret,
        upstream_base_url=upstream_base_url,
        dialect=dialect,
        serve=serve,
    )


def main() -> None:
    try:
        registry = TokenRegistry()
        proxy_app = build_app(os.environ, serve="proxy", registry=registry)
        admin_app = build_app(os.environ, serve="control", registry=registry)
    except UnknownDialectError as exc:
        raise SystemExit(f"forge-llm-proxy: {exc}") from None
    port = int(os.environ.get("FORGE_PROXY_PORT", "8801"))
    admin_port = int(os.environ.get("FORGE_PROXY_ADMIN_PORT", "8802"))
    if admin_port == port:
        raise SystemExit("forge-llm-proxy: the admin port and the proxy port must differ")

    # The admin listener runs in a thread (uvicorn installs signal handlers only on the
    # main thread, so SIGTERM still stops the proxy listener, and the daemon thread dies
    # with the process). If it cannot bind, the worker's health check on it fails and the
    # build ends ENGINE_UNREACHABLE -- the proxy never serves without its control plane.
    admin_server = uvicorn.Server(
        uvicorn.Config(admin_app, host="127.0.0.1", port=admin_port, log_level="warning")
    )
    threading.Thread(target=admin_server.run, name="forge-proxy-admin", daemon=True).start()

    uvicorn.run(proxy_app, host="0.0.0.0", port=port, log_level="warning")  # noqa: S104 - proxy container's whole purpose is to be reached from the sandbox network


if __name__ == "__main__":
    main()
