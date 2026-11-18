"""shared helpers for the `openai` dialect proxy tests (`test_llm_proxy_openai.py`,
`test_build_trace_proxy_openai.py`).

A real proxy app over a stub Responses API. The stub records every request it receives, so a
refusal test can assert that NOTHING reached the upstream, not merely that a 403 came back.
"""

from __future__ import annotations

import json

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from app.services.llm_proxy import TokenRegistry, create_app
from app.services.llm_proxy_openai import OPENAI_DIALECT

REAL_KEY = "sk-proj-REAL-KEY-never-leaves-the-proxy"
ADMIN_SECRET = "test-admin-secret"
ADMIN = {"X-Forge-Admin-Secret": ADMIN_SECRET}


class Upstream:
    """A stub OpenAI API: answers every request with `responses[N]` (the last one repeats) and
    records each one in `calls` as (method, path, headers, body)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict, bytes]] = []
        self.app = FastAPI()

        @self.app.api_route("/{path:path}", methods=["GET", "POST", "DELETE"])
        async def anything(path: str, request: Request) -> Response:
            self.calls.append((request.method, "/" + path, dict(request.headers), await request.body()))
            status, content, content_type = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
            return Response(content=content, status_code=status, media_type=content_type)


def make(responses, *, token_budget=10**9, cost_budget_usd=10**6, http_client=None):
    """(client, token, registry, upstream) for the `openai` dialect."""
    upstream = Upstream(responses)
    registry = TokenRegistry()
    http = http_client or httpx.AsyncClient(
        transport=httpx.ASGITransport(app=upstream.app), base_url="http://upstream.test"
    )
    app = create_app(
        registry, real_api_key=REAL_KEY, admin_secret=ADMIN_SECRET, http_client=http, dialect=OPENAI_DIALECT
    )
    client = TestClient(app)
    token = client.post(
        "/_forge_admin/tokens",
        json={"build_id": "b", "token_budget": token_budget, "cost_budget_usd": cost_budget_usd},
        headers=ADMIN,
    ).json()["token"]
    return client, token, registry, upstream


def usage_obj(input_tokens, output_tokens, *, cached=0, written=0, reasoning=0) -> dict:
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning},
        "total_tokens": input_tokens + output_tokens,
    }


def response_obj(
    output=None, *, model="gpt-5-mini", usage="default", tier="default", status="completed", reason=None
) -> dict:
    body: dict = {
        "id": "resp_1",
        "object": "response",
        "model": model,
        "status": status,
        "output": output if output is not None else [],
        "usage": usage_obj(10, 5) if usage == "default" else usage,
    }
    if tier is not None:
        body["service_tier"] = tier
    if reason is not None:
        body["incomplete_details"] = {"reason": reason}
    return body


def json_reply(output=None, **kw) -> tuple[int, bytes, str]:
    return 200, json.dumps(response_obj(output, **kw)).encode(), "application/json"


def sse(events) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def sse_reply(
    output=None, *, terminal="response.completed", include_output=True, **kw
) -> tuple[int, bytes, str]:
    """A streamed reply: created, one added/done pair per output item, then the terminal event.
    `include_output=False` leaves `output` off the terminal response (the items then reach the
    proxy only through `response.output_item.done`)."""
    final = response_obj(output, **kw)
    items = final["output"]
    events = [{"type": "response.created", "response": {**final, "output": [], "usage": None}}]
    for index, item in enumerate(items):
        events.append({"type": "response.output_item.added", "output_index": index, "item": item})
        if item.get("type") == "function_call":
            events.append(
                {"type": "response.function_call_arguments.delta", "output_index": index, "delta": item["arguments"][:3]}
            )
            events.append(
                {"type": "response.function_call_arguments.done", "output_index": index, "arguments": item["arguments"]}
            )
        events.append({"type": "response.output_item.done", "output_index": index, "item": item})
    if not include_output:
        del final["output"]
    events.append({"type": terminal, "response": final})
    return 200, sse(events), "text/event-stream"


def message_item(text) -> dict:
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def call_item(name, arguments, call_id="call_1") -> dict:
    return {"type": "function_call", "name": name, "arguments": json.dumps(arguments), "call_id": call_id}


def reasoning_item(summary_text=None, encrypted=None) -> dict:
    item: dict = {"type": "reasoning", "summary": []}
    if summary_text is not None:
        item["summary"] = [{"type": "summary_text", "text": summary_text}]
    if encrypted is not None:
        item["encrypted_content"] = encrypted
    return item


def function_tool(name="Read") -> dict:
    return {"type": "function", "name": name, "parameters": {"type": "object", "properties": {}}}


def body(*, drop=(), **over) -> dict:
    """A request body of the allowed shape, with `over` merged in and `drop` removed."""
    base: dict = {
        "model": "gpt-5-mini",
        "input": [{"role": "user", "content": "hi"}],
        "tools": [function_tool()],
        "store": False,
        "service_tier": "default",
    }
    base.update(over)
    for name in drop:
        base.pop(name, None)
    return base


def bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


def send(client, token, payload=None, *, path="/v1/responses", method="POST", raw=None, headers=None):
    """POST `payload` (JSON) or `raw` bytes with the proxy token as the Bearer credential."""
    hdrs = {**bearer(token), **(headers or {})}
    if raw is not None:
        return client.request(method, path, headers=hdrs, content=raw)
    return client.request(method, path, headers=hdrs, json=body() if payload is None else payload)


def usage(client, token) -> dict:
    return client.get(f"/_forge_admin/tokens/{token}/usage", headers=ADMIN).json()


def trace(client, token) -> dict:
    resp = client.get(f"/_forge_admin/tokens/{token}/trace", headers=ADMIN)
    assert resp.status_code == 200
    return resp.json()
