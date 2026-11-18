"""what the PINNED OpenAI SDK puts on the wire, so the `openai` dialect's header and model
allowlists are derived from a measurement and a version bump that changes it is a red test.

The counterpart of `test_every_header_beta_and_model_the_cli_sends_is_one_the_proxy_allows` in
`test_agent_tool_capture_integration.py` (the Claude CLI). The pinned `forge-agent-sandbox-openai`
image runs the REAL staged runner (`app/agent_runner_openai.py`) against a recording server on its
own loopback, with no network at all, so every header it sends is seen raw (before the proxy
filters anything) and no real call is ever made. The server answers `POST /v1/responses` with one
`function_call` and then a final message, like the Responses API, and records every request.

Asserted: nothing the SDK sends is outside the dialect's allowlist (so nothing is silently dropped)
and, on this run, the allowlist is exactly what was sent; no `OpenAI-Organization` /
`OpenAI-Project` is sent; the runner talks to `POST /v1/responses` only; and every model it sends
has a price row.

There is also a second, longer session (`_RICH_RECORDER`: the model answers with a reasoning item, some
text and two parallel function calls, then a reasoning item and two more calls, then a final message)
to measure which INPUT ITEM types the runner replays, because the proxy's input-item allowlist
(`llm_proxy_openai._ALLOWED_INPUT_ITEM_TYPES`) is built from that measurement: every item type on the
wire is on the allowlist and, on this run, every type on the allowlist was sent.

Skips cleanly without Docker or the image (`docker build -t forge-agent-sandbox-openai:latest -f
docker/agent-sandbox-openai/Dockerfile .` from `backend/`).
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound  # noqa: E402

from app.services.llm_proxy_openai import (  # noqa: E402
    OPENAI_INBOUND_HEADERS,
    check_request_policy,
    is_priced_model,
)

IMAGE = "forge-agent-sandbox-openai:latest"
RUNNER_SOURCE = Path(__file__).resolve().parent.parent / "app" / "agent_runner_openai.py"
SANDBOX_UID = 65534


def _image_available() -> bool:
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(IMAGE)
        return True
    except (ImageNotFound, DockerException):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _image_available(), reason=f"requires Docker and {IMAGE} built locally"),
]

# What the proxy never forwards however the SDK sends it: transport, and the credential.
TRANSPORT_AND_CREDENTIAL = {"host", "content-length", "connection", "accept-encoding", "authorization"}

_RECORDER_BODY = r'''
import json, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a):
        pass
    def _rec(self, body):
        print("REC " + json.dumps({"method": self.command, "path": self.path,
              "headers": {k.lower(): v for k, v in self.headers.items()},
              "body": body.decode("utf-8", "replace")}), flush=True)
    def do_GET(self):
        self._rec(b""); self.send_response(404); self.send_header("content-length", "0"); self.end_headers()
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(n)
        self._rec(body)
        if self.path.split("?")[0] != "/v1/responses":
            self.send_response(404); self.send_header("content-length", "0"); self.end_headers(); return
        d = json.loads(body)
        items = d.get("input") if isinstance(d.get("input"), list) else []
        outputs_so_far = sum(1 for i in items if isinstance(i, dict) and i.get("type") == "function_call_output")
        output = turn_output(outputs_so_far)
        resp = {"id": "resp_stub", "object": "response", "created_at": 1760000000, "status": "completed",
                "model": d.get("model"), "output": output, "service_tier": "default", "parallel_tool_calls": True,
                "tool_choice": "auto", "tools": [], "error": None, "incomplete_details": None, "instructions": None,
                "metadata": {}, "temperature": 1.0, "top_p": 1.0, "store": False,
                "usage": {"input_tokens": 10, "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 15}}
        out = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

srv = ThreadingHTTPServer(("127.0.0.1", 8000), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.5)
rc = subprocess.run(["python", "/workspace/.forge/agent_runner_openai.py"]).returncode
print("RUNNER_EXIT", rc, flush=True)
time.sleep(0.3)
'''


# How the recording server answers, by how many `function_call_output` items the request already carries.
# The simple session is the original one (one tool round trip). The RICH one is what makes
# the runner replay every item type a real multi-turn session holds: a reasoning item with encrypted
# content, the assistant's own text, and two PARALLEL function calls.
_SIMPLE_TURNS = r'''
def turn_output(outputs_so_far):
    if outputs_so_far == 0:
        return [{"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "write_file",
                 "arguments": json.dumps({"path": "a.txt", "content": "x"}), "status": "completed"}]
    return [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": "ok", "annotations": []}]}]
'''

_RICH_TURNS = r'''
def turn_output(outputs_so_far):
    if outputs_so_far == 0:
        return [
            {"type": "reasoning", "id": "rs_1", "summary": [{"type": "summary_text", "text": "thinking"}], "encrypted_content": "ENC1"},
            {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": "I will write the file.", "annotations": []}]},
            {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "write_file",
             "arguments": json.dumps({"path": "a.txt", "content": "x"}), "status": "completed"},
        ]
    if outputs_so_far == 1:
        return [
            {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": "ENC2"},
            {"type": "function_call", "id": "fc_2", "call_id": "call_2", "name": "read_file",
             "arguments": json.dumps({"path": "a.txt"}), "status": "completed"},
            {"type": "function_call", "id": "fc_3", "call_id": "call_3", "name": "glob",
             "arguments": json.dumps({"pattern": "*"}), "status": "completed"},
        ]
    return [{"type": "message", "id": "msg_2", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": "ok", "annotations": []}]}]
'''

_SIMPLE_RECORDER = _SIMPLE_TURNS + _RECORDER_BODY
_RICH_RECORDER = _RICH_TURNS + _RECORDER_BODY


def _tar(files: dict[str, bytes]) -> bytes:
    """A tar of `files` (path under /workspace -> bytes), directories and files owned by the
    sandbox uid so the runner can write its result file."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for directory in ("workspace", "workspace/.forge"):
            info = tarfile.TarInfo(directory)
            info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o777, SANDBOX_UID, SANDBOX_UID
            tar.addfile(info)
        for path, data in files.items():
            info = tarfile.TarInfo("workspace/" + path)
            info.size, info.mode, info.uid, info.gid = len(data), 0o644, SANDBOX_UID, SANDBOX_UID
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _record(recorder: str, *, max_turns: int) -> list[dict]:
    """Runs the pinned runner against `recorder` (a server script) with no network and returns every
    request it made, as the recorder printed it."""
    client = docker.from_env()
    task_input = {
        "intent_text": "Create a.txt.",
        "system_prompt": "You are a test.",
        "tools": ["read_file", "write_file", "edit_file", "glob", "grep", "shell"],
        "max_turns": max_turns,
        "model": "gpt-5-mini",
    }
    container = client.containers.create(
        IMAGE,
        ["python", "/workspace/.forge/recorder.py"],
        user=f"{SANDBOX_UID}:{SANDBOX_UID}",
        network_mode="none",
        working_dir="/workspace",
        environment={
            "OPENAI_BASE_URL": "http://127.0.0.1:8000/v1",
            "OPENAI_API_KEY": "proxy-token-not-a-real-key",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        cap_drop=["ALL"],
        security_opt=["no-new-privileges"],
    )
    try:
        container.put_archive(
            "/",
            _tar(
                {
                    ".forge/task_input.json": json.dumps(task_input).encode(),
                    ".forge/agent_runner_openai.py": RUNNER_SOURCE.read_bytes(),
                    ".forge/recorder.py": recorder.encode(),
                }
            ),
        )
        container.start()
        container.wait(timeout=120)
        logs = container.logs().decode("utf-8", "replace")
    finally:
        try:
            container.remove(v=True, force=True)
        except DockerException:
            pass
    assert "RUNNER_EXIT 0" in logs, logs[-2000:]
    records = [json.loads(line[4:]) for line in logs.splitlines() if line.startswith("REC ")]
    assert records, "the recorder saw nothing"
    return records


@pytest.fixture(scope="module")
def recorded() -> list[dict]:
    return _record(_SIMPLE_RECORDER, max_turns=6)


@pytest.fixture(scope="module")
def recorded_rich() -> list[dict]:
    return _record(_RICH_RECORDER, max_turns=8)


def test_the_runner_talks_to_the_responses_endpoint_and_nothing_else(recorded):
    assert [(r["method"], r["path"]) for r in recorded] == [("POST", "/v1/responses"), ("POST", "/v1/responses")]


def test_every_header_the_sdk_sends_is_on_the_dialects_allowlist_and_the_allowlist_is_no_wider(recorded):
    sent = set()
    for rec in recorded:
        sent |= set(rec["headers"])

    forwarded = sent - TRANSPORT_AND_CREDENTIAL
    assert forwarded <= OPENAI_INBOUND_HEADERS, sorted(forwarded - OPENAI_INBOUND_HEADERS)  # nothing dropped
    assert forwarded == OPENAI_INBOUND_HEADERS, sorted(OPENAI_INBOUND_HEADERS - forwarded)  # nothing extra


def test_the_sdk_sends_no_organisation_project_or_beta_header(recorded):
    """Absent unless the process is configured with them, which the runner is not: the allowlist
    leaves them out because the SDK does not send them, and they stay out if someone else does."""
    for rec in recorded:
        assert not {"openai-organization", "openai-project", "openai-beta"} & set(rec["headers"])


def test_every_model_and_body_the_sdk_sends_passes_the_dialects_request_policy(recorded):
    for rec in recorded:
        body = json.loads(rec["body"])
        assert is_priced_model(body["model"]), body["model"]
        assert check_request_policy(rec["body"].encode()) is None
    # the model on the wire is the configured one, not a default of the SDK's
    assert {json.loads(r["body"])["model"] for r in recorded} == {"gpt-5-mini"}


def test_the_measured_run_was_not_vacuous(recorded):
    """Two calls (the tool round trip), each carrying headers and a body."""
    assert len(recorded) == 2 and all(r["headers"] and r["body"] for r in recorded)


def _input_items(records: list[dict]) -> list[dict]:
    items = []
    for rec in records:
        body = json.loads(rec["body"])
        assert isinstance(body["input"], list), body["input"]  # the runner sends a list, never a bare string
        items.extend(body["input"])
    return items


def test_every_input_item_the_runner_replays_is_one_the_dialect_allows_and_none_is_unmeasured(recorded_rich):
    """the OpenAI dialect refuses an input item whose `type` is not on
    `_ALLOWED_INPUT_ITEM_TYPES` (so an `item_reference` is refused). The list is built from THIS run:
    every body passes the dialect's request policy, and the item types on the wire are exactly the
    four on the allowlist -- none sent that is not allowed, none allowed that was never sent. The one
    item with no `type` is the task text, a role message."""
    from app.services.llm_proxy_openai import _ALLOWED_INPUT_ITEM_TYPES

    for rec in recorded_rich:
        assert check_request_policy(rec["body"].encode()) is None

    items = _input_items(recorded_rich)
    typed = {item["type"] for item in items if "type" in item}
    typeless = [item for item in items if "type" not in item]

    measured = {"message", "reasoning", "function_call", "function_call_output"}  # a literal: what this run saw
    assert typed == measured, sorted(typed ^ measured)
    assert _ALLOWED_INPUT_ITEM_TYPES == measured, sorted(_ALLOWED_INPUT_ITEM_TYPES ^ measured)
    assert typeless and all(set(item) == {"role", "content"} and item["role"] == "user" for item in typeless)


def test_the_rich_session_was_not_vacuous(recorded_rich):
    """Three requests; the last replays the whole history (the task text, two reasoning items, the
    assistant's text, three function calls and their three outputs)."""
    assert len(recorded_rich) == 3
    last = json.loads(recorded_rich[-1]["body"])["input"]
    kinds = [item.get("type", "role-message") for item in last]
    assert kinds.count("function_call") == 3 and kinds.count("function_call_output") == 3
    assert kinds.count("reasoning") == 2 and kinds.count("message") == 1 and kinds.count("role-message") == 1


def test_every_top_level_field_the_sdk_sends_is_on_the_dialects_allowlist_and_the_allowlist_is_no_wider(
    recorded, recorded_rich
):
    """the top-level allowlist (`OPENAI_ALLOWED_TOP_LEVEL_FIELDS`) is what the pinned runner
    sends, over both sessions (the tool round trip and the rich one). Nothing sent is refused as an
    unknown field, and no field is allowed that was not sent."""
    from app.services.llm_proxy_openai import OPENAI_ALLOWED_TOP_LEVEL_FIELDS

    sent: set[str] = set()
    for rec in [*recorded, *recorded_rich]:
        sent |= set(json.loads(rec["body"]))
        assert check_request_policy(rec["body"].encode()) is None

    assert sent <= OPENAI_ALLOWED_TOP_LEVEL_FIELDS, sorted(sent - OPENAI_ALLOWED_TOP_LEVEL_FIELDS)
    assert sent == OPENAI_ALLOWED_TOP_LEVEL_FIELDS, sorted(OPENAI_ALLOWED_TOP_LEVEL_FIELDS - sent)
