"""Fake `claude_agent_sdk` used only by the test-only stub sandbox image
(docker/agent-sandbox-stub). Implements just enough of the real package's
surface for `app/agent_runner.py` to run unmodified against it: `query()`,
`ClaudeAgentOptions`, `AssistantMessage`, `TextBlock`, `ResultMessage`.

This lets tests/test_sandbox_build_executor_integration.py exercise the
*real* agent_runner.py -> real SandboxRunner -> real ProxyContainer path
end to end without a real Claude Agent SDK install, a real API key, or any
network egress from inside the sandbox at all — the fake `query()` never
imports httpx/requests and never touches ANTHROPIC_BASE_URL, so it proves
the executor's plumbing independent of engine behavior ("the
engine has no security role").

it WRITES FILES into /workspace, as a real engine would, so
the integration tests see real output travel harvest -> sanitise -> publish ->
image -> policy gate. Before this the stub wrote nothing and the pipeline
published the pre-agent staged directory without any test noticing. What it
writes, by default:
  - app/agent_marker.txt          a marker the pushed commit and the image must contain
  - app/app.py (appended to)      an edit to a file that already existed
  - .forge-decoy.txt              a name that merely resembles the platform's .forge
  - .git/ (a minimal valid repo)  with hooks/pre-commit, a `sh` script that writes
                                  the file named by $FORGE_STUB_HOOK_MARKER if
                                  it is ever RUN. Inside the sandbox that variable
                                  is unset, so the script is inert; on the host it
                                  would be set by the test that watches for it.
                                  It is why the platform never lets host git see
                                  a .git that came out of the sandbox.
  - vendor/lib/.git               a gitlink-style FILE, and vendor/lib/code.py
Directives in the prompt (the request text the test chose) add more:
  [stub:secret]     app/leaked.py holding an AWS-shaped access key
  [stub:protected]  a tampered Dockerfile (a protected path)
  [stub:migration-edit]  appends a comment line to the template's existing migration
                    (app/migrations/versions/d41f9c7a2b3e_create_items_table.py)
  [stub:migration-add]   adds a new, harmless migration under app/migrations/versions/
  [stub:bytecode]   compiled bytecode under the schema prefix: a `cpython-311.pyc` in a
                    `__pycache__` and a source-less `evil.pyc` -- ADR 0032 §Amendment
  [stub:hardlink]   app/alias.txt, a hard link to app/agent_marker.txt
  [stub:symlink]    app/link.txt, a symlink to agent_marker.txt (inside the tree)
  [stub:escape]     app/escape, a symlink to ../../../../etc/passwd (outside it)
  [stub:caserepo]   a COMPLETE repository under `.GIT` (HEAD, objects/, refs/, config,
                    hooks/pre-commit): `.git` to an NTFS host, a separate directory to Linux
  [stub:sparse]     app/sparse.bin, a 3 GiB SPARSE file (it occupies almost nothing in the
                    sandbox, but the workspace archive carries its apparent size)
  [stub:flaky-route]  app/flaky_status.txt: "500" on a first attempt, "200" when the prompt carries
                    the platform's `<measured-failures>` block, i.e. on a repair attempt.
                    The stub is stateless between runs, so the repair prompt's own
                    marker is what tells the two apart. A test-local template whose service
                    answers `GET /flaky` with the status in that file does the rest.
"""

import os
from dataclasses import dataclass

WORKSPACE = "/workspace"
SECRET_LINE = "AWS_ACCESS_KEY_ID = 'AKIAABCDEFGHIJKLMNOP'\n"
MIGRATION_ADD = (
    '"""stub: add a nullable note column (forward-only)"""\n'
    "import sqlalchemy as sa\n"
    "from alembic import op\n\n"
    'revision = "e5f6a7b8c9d0"\n'
    'down_revision = "d41f9c7a2b3e"\n'
    "branch_labels = None\n"
    "depends_on = None\n\n\n"
    "def upgrade():\n"
    '    op.add_column("items", sa.Column("note", sa.Text(), nullable=True))\n\n\n'
    "def downgrade():\n"
    "    pass\n"
)
TAMPERED_DOCKERFILE = 'FROM alpine:3.19\nCMD ["echo", "AGENT TAMPERED"]\n'
HOOK = (
    "#!/bin/sh\n"
    '[ -n "$FORGE_STUB_HOOK_MARKER" ] && echo "pre-commit hook ran" > "$FORGE_STUB_HOOK_MARKER"\n'
    "exit 0\n"
)


class TextBlock:
    def __init__(self, text: str):
        self.text = text


class AssistantMessage:
    def __init__(self, content):
        self.content = content


@dataclass
class ResultMessage:
    is_error: bool
    total_cost_usd: float
    usage: dict


class ClaudeAgentOptions:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _write(rel: str, data: str, mode: int | None = None, append: bool = False) -> None:
    path = os.path.join(WORKSPACE, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a" if append else "w", encoding="utf-8") as f:
        f.write(data)
    if mode is not None:
        os.chmod(path, mode)


def _act_on_workspace(prompt: str) -> None:
    _write("app/agent_marker.txt", "AGENT-MARKER written by the stub engine\n")
    if os.path.exists(os.path.join(WORKSPACE, "app", "app.py")):
        _write("app/app.py", "\n# edited by the stub agent\n", append=True)
    _write(".forge-decoy.txt", "not the platform's .forge\n")
    # A minimal but VALID git repository (HEAD, objects/, refs/), so that if host
    # git were ever run in it, it would treat it as a repository and run the hook.
    _write(".git/HEAD", "ref: refs/heads/main\n")
    os.makedirs(os.path.join(WORKSPACE, ".git", "objects"), exist_ok=True)
    os.makedirs(os.path.join(WORKSPACE, ".git", "refs"), exist_ok=True)
    _write(".git/hooks/pre-commit", HOOK, mode=0o755)
    _write("vendor/lib/.git", "gitdir: ../../../elsewhere\n")
    _write("vendor/lib/code.py", "VALUE = 1\n")
    if "[stub:secret]" in prompt:
        _write("app/leaked.py", SECRET_LINE)
    if "[stub:protected]" in prompt:
        _write("Dockerfile", TAMPERED_DOCKERFILE)
    if "[stub:migration-edit]" in prompt:
        _write(
            "app/migrations/versions/d41f9c7a2b3e_create_items_table.py",
            "\n# edited by the stub agent\n",
            append=True,
        )
    if "[stub:migration-add]" in prompt:
        _write("app/migrations/versions/e5f6a7b8c9d0_stub_add_note.py", MIGRATION_ADD)
    if "[stub:bytecode]" in prompt:
        _write("app/migrations/versions/__pycache__/d41f9c7a2b3e_create_items_table.cpython-311.pyc", "BYTECODE")
        _write("app/migrations/versions/evil.pyc", "SOURCE-LESS BYTECODE")
    if "[stub:caserepo]" in prompt:
        _write(".GIT/HEAD", "ref: refs/heads/main\n")
        os.makedirs(os.path.join(WORKSPACE, ".GIT", "objects"), exist_ok=True)
        os.makedirs(os.path.join(WORKSPACE, ".GIT", "refs"), exist_ok=True)
        _write(".GIT/config", "[core]\n\tfsmonitor = echo hostile\n[user]\n\tname = Evil\n\temail = evil@example.invalid\n")
        _write(".GIT/hooks/pre-commit", HOOK, mode=0o755)
    if "[stub:flaky-route]" in prompt:
        _write("app/flaky_status.txt", "200\n" if "<measured-failures>" in prompt else "500\n")
    if "[stub:sparse]" in prompt:
        with open(os.path.join(WORKSPACE, "app", "sparse.bin"), "wb") as sparse:
            sparse.truncate(3 * 1024 * 1024 * 1024)
    if "[stub:hardlink]" in prompt:
        os.link(os.path.join(WORKSPACE, "app", "agent_marker.txt"), os.path.join(WORKSPACE, "app", "alias.txt"))
    if "[stub:symlink]" in prompt:
        os.symlink("agent_marker.txt", os.path.join(WORKSPACE, "app", "link.txt"))
    if "[stub:escape]" in prompt:
        os.symlink("../../../../etc/passwd", os.path.join(WORKSPACE, "app", "escape"))


async def query(*, prompt: str, options=None):
    """Writes files into /workspace (see the module docstring), then yields a
    canned AssistantMessage + ResultMessage, mirroring the shape
    app/agent_runner.py expects from the real SDK."""
    _act_on_workspace(prompt)
    yield AssistantMessage(content=[TextBlock(text=f"fake agent completed: {prompt}")])
    yield ResultMessage(
        is_error=False,
        total_cost_usd=0.0123,
        usage={"input_tokens": 111, "output_tokens": 222},
    )
