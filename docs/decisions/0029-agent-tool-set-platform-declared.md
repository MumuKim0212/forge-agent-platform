# 0029. The build agent's tool set is platform-declared, and the one thing the sandbox cannot contain is refused at the LLM proxy

- Status: accepted
- Date: 2026-09-30
- Amends: the earlier decision that put the agent engine behind an adapter (its Consequences on engine tool access). Builds on ADR 0003 and ADR 0014.

## Context

That earlier adapter decision and the adapter contract in the project's build documentation both say engine tool access "is constrained via an `allowed_tools` allowlist + `can_use_tool` callback". `app/agent_runner.py` implements neither: it passes `permission_mode="bypassPermissions"` and no tool option at all, with a comment citing the adapter decision. An amendment of 2026-09-11 to that decision recorded the divergence and deferred it to this ADR.

Neither side of the divergence is right as written.

- **The code's premise is mostly sound.** L3 exists to execute untrusted code (ADR 0003). Anything the agent can do *inside* the sandbox with a tool (read, write, run a shell) generated code can already do at build or preview time. The sandbox, not the engine, is the boundary for local effects; that is the adapter decision's own principle that security must not depend on the engine.
- **But the premise has one hole the sandbox cannot close by construction: server-side tools.** A tool the API provider executes (web search, web fetch, the Messages API's MCP connector, code execution) reaches the internet *from the provider's side*, and its request travels over the one path invariant 3 deliberately leaves open: sandbox → LLM proxy → upstream. Default-deny egress is irrelevant to it. It is both an exfiltration channel (the query carries whatever the agent puts in it) and an injection channel (fetched content returns into the agent's context). Nothing in L3 can see it; only the proxy can.
- **The docs' mechanism does not do what the docs say.** In the Claude Agent SDK, `allowed_tools` pre-approves the listed tools rather than removing the others, and under `bypassPermissions` restricts nothing. `can_use_tool` requires a streaming session (the runner passes a plain string prompt), and it is a callback running in the same untrusted container as the agent: policy logic in the runner, which the adapter decision forbids, guarding nothing a cooperative model would not already respect. *(These SDK semantics are an unverified reading and must be checked against the pinned SDK's source by the implementation; see §Consequences.)*
- **The SDK and CLI are unpinned** (`pip install claude-agent-sdk`, `npm install -g @anthropic-ai/claude-code` with no version). Any verified property of the option set holds only until the next image rebuild.
- **Workspace content is untrusted input.** A modify-type build stages a repository earlier agents wrote. If the engine reads project-level configuration from the workspace (settings, hooks, MCP server declarations, memory files), a previous build can change the next build's tool set or permission posture.

## Decision

**1. The tool set is a platform declaration, composed on the host and carried in the task input.** Same shape as the adapter decision's rule for policy text: one source in `app/`, forwarded by the runner verbatim, never authored by it. The runner passes it to the engine through the option that **makes unlisted tools unavailable**, not the one that auto-approves listed ones. The declared set is the file tools plus a shell; it excludes every tool whose effect leaves the sandbox (web search, web fetch, any MCP tool). The shell stays: executing code is what L3 is for.

**2. The LLM proxy refuses any request that asks the provider to act outside the sandbox.** This is the enforced half, in L2, owned by the platform, independent of the engine. The engine-level declaration in (1) is defence-in-depth behind it, not the other way round. The rule is an **allowlist of request shapes**, never a denylist of known tool names:
   - every entry in `tools` must be a client-executed tool definition (no `type`, or `type: "custom"`); any other `type` is refused;
   - a request carrying `mcp_servers` (or any other top-level field that makes the provider call out) is refused;
   - a refused request is never forwarded upstream, is never silent (it reaches the build's failure message), and fails the build closed.

**3. Workspace-carried engine configuration is not loaded.** The runner sets the engine's configuration sources explicitly to none, and declares no MCP servers, so nothing in the staged workspace can alter the tool set, the permission mode, or hooks.

**4. The agent image pins exact SDK and CLI versions.** Tool-option semantics are version-dependent; pinning is what makes the verification in (1) and (3) mean something beyond the day it was run. Upgrading either is an explicit change that re-runs that verification.

**5. `permission_mode="bypassPermissions"` stays.** A build has no human to answer a prompt, and with (1)-(3) in place the mode governs only whether an available tool asks first, not which tools exist. Its code comment cites this ADR.

**6. `can_use_tool` is not used.** It is removed from the docs rather than implemented.

## Alternatives considered

- **Correct the docs only (the engine has no security role; delete the allowlist claim).** Honest about the local case, but silently accepts the server-side-tool channel, which is the one case where the engine's tool set is the *only* thing between L3 and the internet today.
- **Implement the docs as written (`allowed_tools` + `can_use_tool`).** `allowed_tools` does not restrict; `can_use_tool` moves policy into the untrusted container and forces a streaming session. Implements the words while protecting nothing.
- **Remove the shell from the tool set.** Buys nothing the sandbox does not already provide, and costs the agent the ability to run the tests it writes.
- **Proxy strips disallowed tools instead of refusing.** Mutating requests in flight makes the engine's view and the provider's view disagree silently; refusal is fail-closed and visible.
- **Proxy denylist of known server-tool names.** A denylist silently admits the next server tool the provider ships (a durable project rule: exposure is an allowlist, never a serialization).

## Consequences

- Gain: the one L3 → internet path that did not depend on the engine's goodwill now does not depend on it at all; the docs describe the code; the verification survives image rebuilds.
- Give up: a future feature that legitimately wants a server-side tool (e.g. a tool registry) must widen the proxy's allowlist in an ADR, deliberately; the proxy rule is where that decision will be made.
- Watch out: the SDK semantics stated in §Context are a lead, not a fact. The implementation verifies them against the pinned version's source and, where it can, by capturing the request the real CLI actually sends. If they differ, the Decision's *intent* (unlisted tools unavailable; workspace config ignored) governs and the mechanism adapts.
- Watch out: the proxy refusal applies to every build, so a CLI upgrade that starts sending a new tool shape will fail builds closed until the allowlist is reviewed. That is the intended failure direction.
- Not addressed: the system prompt and intent text themselves (`agent_prompt.py`, `sandbox_build_executor.py`) are unchanged by this ADR.

## Amendment 2026-09-30 — the allowlist covers the endpoint, not only the body

A check of the first implementation found §2 bypassable. The proxy forwarded **every path**, and the body rule inspected only the top level of the body. `POST /v1/messages/batches` carries full Messages requests nested under `requests[].params`, including `tools` and `mcp_servers`. A server-tool request sent that way returned 200 and reached upstream with the real key (measured). The agent has a shell and the proxy token, so it does not need the engine's cooperation to send one. The Batches, Files and other provider endpoints each create channels the body rule was never shaped to read.

**Decision: the proxy forwards only the (method, path) pairs the pinned CLI is measured to use.** Every other endpoint is refused before forwarding, with its own `detail`. The body rule then applies to those endpoints, whose body shapes are known. This is the same allowlist principle §2 already states, applied one level up: an allowlist of body shapes on an unbounded set of endpoints is not an allowlist. Widening the endpoint set follows the same rule as widening the tool-type set. It is an explicit change, re-measured against the capture.

**Two clarifications from the same measurements:**
- **`tools` bounds only the built-in tools.** In the pinned SDK/CLI (0.2.162 / 2.1.285), MCP tools from any configured source are added on top of the `tools` list. §1's intent therefore depends on §3 as well: with `setting_sources=[]` a workspace `.mcp.json` does not connect, and without it the capture showed a workspace-declared MCP tool on the wire. The runner also sets `strict_mcp_config=True`.
- **"Any other `type`" also refuses Anthropic-defined *client* tools** (`bash_*`, `text_editor_*`, `computer_*`). The pinned CLI sends none. This is intended: a future feature that wants one widens the allowlist in an ADR, like any server tool.

## Amendment 2026-10-11 — the OpenAI engine (ADR 0035)

The same rules hold per engine:
- **Tool set.** It is declared on the host, per engine. For `openai` it is the runner's own function tools (file read/write/edit/glob/grep and a shell).
- **Proxy.** The `openai` dialect forwards only `POST v1/responses` with `type: "function"` tools. It refuses any hosted tool type and any server-side state field (`previous_response_id`, `conversation`, `background`, `prompt`), and it requires `store: false` and `service_tier: "default"`. This is an allowlist of shapes, as before.
- **No workspace configuration** is loaded by the OpenAI runner.

## Amendment 2026-10-24: OpenAI input items are allowlisted by type

The OpenAI dialect admits only the input item types the pinned runner was measured to send: `message`, `reasoning`, `function_call`, `function_call_output`, plus a typeless role message. Anything else is a recorded policy refusal. That includes `item_reference` (it reads a provider-stored item by id, the same server-side-state class as `previous_response_id` and `conversation`) and `computer_call_output`. The measurement is re-derived on every Docker run by `test_openai_sdk_wire_integration.py`. A type the pinned SDK starts sending in a later version fails closed, naming the type, until it is measured and added.
