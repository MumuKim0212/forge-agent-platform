# 0032. The agent's harvested output is what gets published — and it crosses into L2 as untrusted input

- Status: accepted
- Date: 2026-10-05
- Builds on ADR 0003. Constrains the agent adapter contract.

## Context

The build pipeline is supposed to publish what the agent produced (build container lifecycle: provision → execute → **harvest** → destroy). Found while scoping a later change and confirmed both by an execution probe and by an independent read of the code: **it never has.**

- `SandboxRunner` extracts the container's `/workspace` into a fresh `forge-harvest-*` temp directory.
- `AgentAdapter` reads only `.forge/result.json` from that directory and does not return it.
- `SandboxBuildExecutor.execute` passes the **pre-agent staged directory** to `_publish`: the template skeleton for a create build, the confirmed ref for a modify build.

So since that pipeline first landed:

- every PR commit and every image has been the pre-agent tree;
- every policy verdict has been computed over code the agent never touched;
- the platform's own `.forge/task_input.json` (the system prompt) and `.forge/agent_runner.py` have been committed and pushed;
- every harvested directory, which holds the agent's real output, has been left behind in the host's temp directory.

Nothing failed, because nothing in the suite has an agent write a file and then looks for it downstream. The stub engine writes nothing, and the integration test planted its "agent output" straight into the staged directory.

Fixing the hand-off is not just a variable rename. Today every host-side tool (git, docker build, the policy checks) operates on a directory the host wrote. Publishing the harvested output means handing **L3-authored bytes** to host tooling for the first time. That is the boundary ADR 0003 draws, crossed in the opposite direction from the one it guards. The sharpest consequence: an agent that creates `.git/hooks/pre-commit` (or a `.git/config` with `core.fsmonitor`/`core.hooksPath`) in its workspace would have the host's `git commit` run that code **outside the sandbox**, if publish committed the harvested directory in place.

## Decision

1. **The harvested output is the build's output.** The adapter returns the harvested directory. Publish, image assembly and the policy gate all operate on the output derived from it. The staged directory is input only.
2. **The harvested tree crosses into L2 only through a sanitising copy into a fresh, host-created directory.** Host tooling never runs in the extracted tree itself.
   - **Regular files and directories are copied.**
   - **`.forge/` at the root is never copied.** It is the platform's own channel into and out of the sandbox, read only for `result.json`.
   - **Any entry named `.git` at any depth is never copied.** Git metadata from L3 is never interpreted by host git. The fresh directory gets its repository from the platform's own `git init`, as today.
   - **Any symbolic link, hard link, device, FIFO or other non-regular entry refuses the build** (`HARVEST_ERROR`, with a platform-template detail naming the kind and the count). A generated web tool has no use for them. Following one from host code, for example a policy check opening a file, could read host files into a verdict. Refusing is visible; silently dropping would change the agent's output without telling anyone.
3. **The protected-path restore and the image's pinned Dockerfile apply to the sanitised copy**, as they were always meant to.
4. **Every temporary tree is removed when the build ends**, success or failure: staged, harvested and sanitised.
5. **The adapter contract gains the output location as an explicit field.** "Task input → code-change output" now names the change: the harvested tree, not a side effect on the input directory.

## Alternatives considered

- **Extract the harvest back into the staged directory.** This is one line. But host git would then run in a tree containing whatever `.git` the agent wrote, which is host code execution from L3. It also mixes platform-written and agent-written bytes in one place.
- **Publish the harvested directory in place, but run git with `-c core.hooksPath=` and similar flags.** That closes known vectors one flag at a time. Git has many configuration-driven execution paths (fsmonitor, filters, credential helpers, `includeIf`), and a denylist of them is the shape this project's design rules forbid. Not reading L3 git metadata at all is an allowlist.
- **Drop links silently.** Rejected for the reason in §Decision 2.

## Consequences

- Gain: builds publish what the agent built, so the platform's core function works for the first time. The policy gate evaluates real generated code. No L3 bytes are ever interpreted as host configuration.
- Give up: an agent that legitimately wanted a symlink or a nested git repository cannot have one. Neither fits the golden-path template.
- Watch out: **the policy gate has been rubber-stamping the template.** Once real output flows, builds will start being flagged and routed to approvers for the first time. That is the gate working, and approver load is about to be measured for the first time.
- Watch out: a `.gitattributes` in the output still reaches host git and can name a filter. Filters run only if defined in configuration, and the platform's fresh repository defines none. Global host git configuration is the operator's.
- Watch out: the leaked `forge-harvest-*` directories from past builds are on the host now, in the system temp directory. They hold agent output from every real build so far. Removing them is an operator action.

## Amendment 2026-10-05 — compiled bytecode is never published

The host's template directory holds `__pycache__/*.cpython-314.pyc`, written when the test suite imported the template. Create-build staging copies `app/` wholesale, so those files have been committed into every new service and are part of every confirmed ref.

This matters for the modify-build schema check, which refuses any added file under `app/migrations/`. A 3.11 sandbox that imports a migration writes `cpython-311.pyc` there, which would refuse a correct build. Exempting bytecode from the check is not the fix: a source-less `.pyc` is importable in place of the source it claims to be, so an exemption would let an agent change what a migration *does* without changing its source.

**Decision:** compiled Python bytecode never crosses into a published tree.
- The sanitising copy skips every `__pycache__` directory, and every `*.pyc` / `*.pyo` file, at any depth. Name matching follows §Decision 2's casefold rule.
- Create-build staging excludes them from the skeleton copy.
- The agent's sandbox runs with `PYTHONDONTWRITEBYTECODE=1`, so they are rarely produced at all.

The runtime recompiles from source, so nothing is lost. The skip is silent rather than a refusal, because bytecode is a by-product of running code, not something the agent chose to publish.


## Amendment 2026-10-17: ignore rules never split the commit from the image

Publish commits with `git add -A`, which honours a `.gitignore` in the harvested tree and the host's global `core.excludesFile`. Image assembly builds from the directory. A code review on 2026-10-16 pointed out what that allows: an agent that writes `app/hook.py` plus a `.gitignore` listing it ships a file in the deployed image that the PR, the reviewed diff and `confirmed_ref` never contain, and the next modify build silently drops it. A benign global ignore rule does the same by accident.

**Decision.** The commit and the image are built from the same file set. Publish adds every harvested file regardless of ignore rules, its own or the host's (a forced add with an empty `core.excludesFile`), or the image is built from the published commit. The implementation picks one and pins it with a test that plants an ignored file. `.gitignore` itself stays an ordinary harvested file (it is copied deliberately, `tests/test_harvest.py`); it simply stops having an effect on what is published.


## Amendment 2026-10-28: same bytes, not only the same files

The amendment above closed the file SET. The same split exists for content: `git add` converts what it stores -- by attribute (a `.gitattributes` in the harvested tree, which the agent writes, `$GIT_DIR/info/attributes`, the host's attributes file: `text eol=...`, `working-tree-encoding`, `ident`, `filter=<a host-configured driver>`) and by host configuration (`core.autocrlf`) -- while the image is built from the raw file. A PR reviewer, the GitHub view and `confirmed_ref` could then show other bytes than the ones deployed (the platform's own diff and the policy gate read the raw tree and were not fooled). **Decision (2026-10-28).** Publish commits the harvested bytes exactly. While it adds the tree it replaces `$GIT_DIR/info/attributes` -- the highest-precedence attribute source, and one a host `init.templateDir` can fill -- with a single line that unsets `text`, `filter`, `ident` and `working-tree-encoding` for every path; `core.autocrlf`, `core.eol` and `core.safecrlf` act only on a path whose `text` is not unset, so they are neutralised with it. An agent-written `.gitattributes` is still committed as an ordinary file (the commit must equal the workspace); it simply has no effect at publish, as `.gitignore` has none. The proof is bytes, not names: after staging, `_assert_index_is_the_workspace` compares every index entry's object id with `git hash-object --no-filters` of the file on disk and fails the build (`HARVEST_ERROR`, nothing pushed) on any difference, naming only counts. **Named exceptions.** The executable bit: the platform repository's `core.fileMode` is probed from the filesystem at `git init`, so on a host whose filesystem has no execute bit (measured on a Windows development host: `core.fileMode=false`) every file is committed `100644` while docker-py marks every file `0755` in the image context; on Linux git records the owner execute bit and the image keeps the file's mode, so the two agree on it (not measured on a Linux host). Permission bits other than the owner execute bit are in the image and not in the commit on every host. Symlinks do not arise: harvest refuses them (`HARVEST_ERROR`), and the byte check still compares one by its target text.
