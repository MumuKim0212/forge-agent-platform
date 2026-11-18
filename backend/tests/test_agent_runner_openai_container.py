"""the OpenAI runner's workspace confinement, exercised inside a real container.

The symlink-escape cases in `test_agent_runner_openai.py` need `os.symlink`, which an
unprivileged Windows host cannot do, so they skip there. This file runs the SAME tool functions
(the real `app/agent_runner_openai.py` source, handed to the container as text) inside
the pinned OpenAI sandbox image, where symlinks and bash exist, and asserts on what came back:
every escape refused, the file outside untouched, the shell run in the workspace with neither
the proxy URL nor the proxy token in its environment.

Skips cleanly without Docker or `forge-agent-sandbox-openai:latest`
(`docker build -t forge-agent-sandbox-openai:latest -f docker/agent-sandbox-openai/Dockerfile .`
from `backend/`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("docker")

import docker  # noqa: E402
from docker.errors import DockerException, ImageNotFound  # noqa: E402

IMAGE = "forge-agent-sandbox-openai:latest"
RUNNER_SOURCE = Path(__file__).resolve().parent.parent / "app" / "agent_runner_openai.py"


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

_SCRIPT = r'''
import json, os, sys, types
src = os.environ["RUNNER_SRC"]
mod = types.ModuleType("agent_runner_openai")
exec(compile(src, "agent_runner_openai.py", "exec"), mod.__dict__)

ws = "/tmp/ws"
outside = "/tmp/outside"
os.makedirs(ws + "/src"); os.makedirs(outside)
open(outside + "/secret.txt", "w").write("OUTSIDE-SECRET\n")
open(ws + "/src/app.py", "w").write("print('hello')\nTODO = 1\n")
os.symlink(outside + "/secret.txt", ws + "/link.txt")      # a file link out
os.symlink(outside, ws + "/escape")                         # a directory link out
os.symlink(ws + "/src/app.py", ws + "/alias.py")            # a link that stays inside
mod.WORKSPACE = ws

out = {}
def attempt(name, fn):
    try:
        out[name] = ["ok", fn()]
    except mod.ToolRefused as e:
        out[name] = ["refused", str(e)]
    except Exception as e:
        out[name] = ["error", type(e).__name__ + ": " + str(e)]

attempt("read_file_link", lambda: mod.read_file("link.txt"))
attempt("write_file_link", lambda: mod.write_file("link.txt", "overwritten"))
attempt("edit_file_link", lambda: mod.edit_file("link.txt", "OUTSIDE", "CHANGED"))
attempt("read_dir_link", lambda: mod.read_file("escape/secret.txt"))
attempt("write_dir_link", lambda: mod.write_file("escape/new.txt", "planted"))
attempt("traversal", lambda: mod.read_file("../outside/secret.txt"))
attempt("absolute", lambda: mod.read_file(outside + "/secret.txt"))
attempt("glob", lambda: mod.glob_files("**/*"))
attempt("grep", lambda: mod.grep_files("SECRET"))
attempt("read_inside_alias", lambda: mod.read_file("alias.py"))
os.environ["OPENAI_API_KEY"] = "forge-proxy-token-xyz"
os.environ["OPENAI_BASE_URL"] = "http://10.0.0.1:8801/v1"
attempt("shell", lambda: mod.run_shell("pwd; echo key=[$OPENAI_API_KEY] url=[$OPENAI_BASE_URL]; exit 3"))
out["outside_after"] = open(outside + "/secret.txt").read()
out["outside_files"] = sorted(os.listdir(outside))
print("RESULT " + json.dumps(out))
'''


@pytest.fixture(scope="module")
def results() -> dict:
    client = docker.from_env()
    logs = client.containers.run(
        IMAGE,
        ["python", "-c", _SCRIPT],
        environment={"RUNNER_SRC": RUNNER_SOURCE.read_text(encoding="utf-8")},
        remove=True,
        stdout=True,
        stderr=True,
    ).decode("utf-8", "replace")
    line = next(ln for ln in logs.splitlines() if ln.startswith("RESULT "))
    return json.loads(line[len("RESULT ") :])


@pytest.mark.parametrize(
    "name",
    ["read_file_link", "write_file_link", "edit_file_link", "read_dir_link", "write_dir_link", "traversal", "absolute"],
)
def test_every_escape_is_refused_inside_the_container(results, name):
    assert results[name] == ["refused", results[name][1]]
    assert "outside the workspace" in results[name][1]


def test_nothing_outside_the_workspace_was_read_written_or_created(results):
    assert results["outside_after"] == "OUTSIDE-SECRET\n"
    assert results["outside_files"] == ["secret.txt"]  # `escape/new.txt` was never created through the link


def test_glob_and_grep_do_not_follow_links_out(results):
    assert results["glob"][0] == "ok"
    listed = results["glob"][1].splitlines()
    assert "src/app.py" in listed
    assert not any(p.startswith("escape") or p == "link.txt" for p in listed)
    assert "OUTSIDE-SECRET" not in results["grep"][1]


def test_a_link_that_stays_inside_the_workspace_is_readable(results):
    assert results["read_inside_alias"][0] == "ok" and "hello" in results["read_inside_alias"][1]


def test_the_shell_runs_in_the_workspace_with_neither_the_proxy_url_nor_token(results):
    kind, text = results["shell"]
    assert kind == "ok"
    assert text.startswith("exit code 3\n/tmp/ws\n")
    assert "key=[] url=[]" in text
