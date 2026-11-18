# 0035. OpenAI is a second agent engine, selected by an admin, and the proxy speaks one dialect per engine

- Status: accepted
- Date: 2026-10-11
- Amends: ADR 0014, ADR 0029, and the earlier decisions that put one engine behind an adapter and kept credentials and models in `.env` (narrowed here for one value, the engine choice). Widens the admin console's writable set.

## Context

The platform's operator has an OpenAI API key rather than an Anthropic one, so OpenAI is to be added as a second engine that an admin can switch to whenever they choose. Today every engine-shaped piece is Anthropic-only:

- **The runner** is the Claude Agent SDK driving the pinned Claude CLI, and the sandbox image installs both.
- **The adapter** hard-codes `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY` into the sandbox environment.
- **The LLM proxy** understands only the Messages API: its endpoint allowlist, request-shape policy, `x-api-key` injection, SSE usage parsing, price table and trace parser.
- **The engine is chosen once, at worker start** (`_select_executor`). The API's `_initial_build_status` uses the same "key set and opt-in" test.

The earlier adapter decision said "one engine behind the adapter, no multi-engine framework". That was a scoping call, not a security property. The security properties live in the sandbox and the proxy (ADR 0003, 0014, 0029), and they must hold unchanged for any engine:
- the real key never enters L3;
- egress goes only to the proxy;
- the proxy is the only cost source, and it fails closed;
- the tool set is platform-declared;
- server-side tools are refused at the proxy;
- workspace configuration is never loaded.

Research on 2026-10-11 compared the OpenAI options:
- **Codex CLI.** Its defaults include a hosted web search, connector apps, `AGENTS.md` loading from the workspace with no documented off switch, WebSocket transport, and a compaction endpoint. Its own sandbox does not run in an unprivileged container, and its tool set is defined by Codex.
- **OpenAI Agents SDK (Python).** It sends only the function tools the caller registers, reads nothing from the workspace or the home directory, uses plain HTTP to the Responses API, and lets tracing upload be disabled.

## Decision

1. **The engine set is closed: `anthropic` and `openai`.** It is not a plugin framework. Each engine has its own runner script (staged from the host per run), its own pinned sandbox image, its own declared tool set, and its own proxy dialect. Adding a third engine is an ADR, not configuration.
2. **The OpenAI runner uses the OpenAI Agents SDK, pinned, and implements the tools itself as function tools.**
   - The tool set is declared on the host: file read, write, edit, glob and grep, plus a shell, mirroring the Anthropic set's capability.
   - Tools execute inside the sandbox, where the sandbox contains them.
   - The runner sets `store=false`, an explicit `service_tier` of `"default"`, the turn limit, and the model, all from the task input.
   - It disables SDK tracing in code and by environment variable.
   - It reads nothing from the workspace except the files its tools are asked for.
   - It writes `.forge/result.json` in the existing shape. Nothing reads its usage figures for cost (ADR 0014 §Amendment).
3. **The proxy has one dialect per engine.** The core is engine-neutral: the token registry, budget, unmetered fail-closed, admin endpoints, trace storage and bounds. A dialect supplies the engine-specific parts:

   | Part | `anthropic` (unchanged) | `openai` |
   |---|---|---|
   | Endpoint allowlist | `POST v1/messages` (`?beta=true`) | `POST v1/responses` only (no compact, no conversations, no files) |
   | Upstream auth | `x-api-key` | `Authorization: Bearer` |
   | Tool shapes allowed | no `type` / `custom` | `type: "function"` only |
   | Top-level fields | an allowlist of the ten the pinned CLI was measured to send: `model`, `messages`, `system`, `tools`, `metadata`, `max_tokens`, `thinking`, `context_management`, `output_config`, `stream`; any other field is refused (`mcp_servers`, `container`, `inference_geo`, `service_tier`, `speed`, `tool_choice`, ...) | an allowlist of the seven the pinned runner was measured to send: `model`, `input`, `instructions`, `tools`, `store`, `service_tier`, `include`; any other field is refused (`previous_response_id`, `conversation`, `background`, `prompt`, `stream`, `tool_choice`, ...) |
   | Refused values | any tool `type` other than none / `custom`; a `url` or `file` content source | any non-function tool; `store` absent or not `false`; `service_tier` absent or not `"default"`; `include` that is null, not a list, or holds any value outside an explicit allowlist (today only `reasoning.encrypted_content`, which `store: false` needs for multi-turn reasoning); an `input` item outside the measured four types |
   | Usage source | `message_start` / `message_delta` | `response.usage`, in `response.completed`, `response.incomplete` or `response.failed` (SSE), or the top-level JSON |
   | Trace parse | content blocks | output items: `message` text, `function_call` (requested), `reasoning` (presence and length only); input `function_call_output` of the last turn (reported) |

   The proxy container is started with one dialect and that dialect's key. A proxy never holds both keys.
4. **OpenAI metering.**
   - `input_tokens` includes cached input. Cost is uncached input at the input rate, plus `cached_tokens` at the cached rate, plus `output_tokens` at the output rate. Reasoning tokens are inside `output_tokens`.
   - The token counter is uncached input plus output (cached reads are not counted, as in ADR 0014 §Amendment point 4).
   - The price is taken from the response's `model` and `service_tier`:
     - `default` at the listed rates;
     - `priority` or `fast` at 2×;
     - `flex` at 0.5×;
     - any other tier, or none reported, at the highest tier multiplier (2×).
   - A dated snapshot the API reports (`<model>-YYYY-MM-DD`, e.g. `gpt-5-mini-2025-08-07`) is priced at its base model's row. Any other suffix is an unknown model.
   - `cache_write_tokens` is priced at the input rate for the listed models, which publish no separate write rate.
   - The price table is code, citing its source and date. Unknown models are charged at the most expensive row.
   - An unmeterable 2xx fails closed exactly as for Anthropic.
5. **The selected engine is DB state, set by an admin, and read per build.**
   - **Storage.** It is a single platform-level row holding the engine. It is not a `Settings` field. Credentials and models stay in `.env`, as before: `OPENAI_API_KEY`, `OPENAI_MODEL` and the OpenAI image are environment values. Only the *choice* between configured engines moves to the database.
   - **Availability.** An engine is *available* when its key is configured. Only an available engine can be selected (otherwise 422). With no row, the operator's `AGENT_ENGINE_DEFAULT` applies if set; otherwise the default is the only available engine, or `anthropic` when both are. An admin's stored choice always wins over the environment default.
   - **Who and how.** The change is made by an admin through an endpoint outside `/api/console` (the console router stays read-only). It is audited with the old and new value, like a quota or role change.
   - **When it applies.** The worker resolves the engine when it **claims** a build. The build records the engine and the model it ran with, as measured facts. A running build is never switched.
   - **No silent fallback.** If the selected engine is not available at claim time, for example because its key was removed, the build fails with a platform-authored detail naming that. Running the build on the other engine would change the vendor and the cost without anyone choosing it.
   - **The opt-in is engine-neutral.** The executor is active when `USE_SANDBOX_EXECUTOR` is set and at least one engine is available. The API's initial build status uses the same test.
6. **Per-build budgets are shared.** `BUILD_COST_BUDGET_USD` and `BUILD_TOKEN_BUDGET` apply whichever engine runs. Cost rollups are engine-agnostic sums.
7. **Policy check secret patterns gain OpenAI key shapes** (`sk-proj-…` and similar), beside the existing Anthropic pattern.

## Alternatives considered

- **Codex CLI as the OpenAI runtime.** Rejected for the reasons in §Context. Each default would need its own disabling flag, and a denylist of a third party's features is the shape this project's design rules forbid.
- **One proxy holding both keys and routing by path.** One container per build is simpler. But a proxy holding a key the build cannot use is a credential exposed for nothing, and a misroute would bill the wrong vendor.
- **Engine choice as a `.env` value (restart to switch).** It fits the existing `.env` configuration approach unchanged. The requirement is that an admin can switch at will, so a restart-only switch does not meet it.
- **A per-project engine.** More flexible, but it doubles every cost and quality comparison and is not required. Revisit with the eval pipeline.

## Consequences

- Gain: the platform runs on either vendor, and an operator chooses without a restart or a deploy.
- Gain: the proxy's engine-neutral core becomes explicit, so the security properties are stated once and tested per dialect.
- Give up: two runners, two images, two dialects and two price tables to keep current. A pinned-version bump on either side is a deliberate change with its engine-up test.
- Watch out: OpenAI model names and prices change. The engine-up test must assert that the model actually sent has its own price row, as the Anthropic capture does.
- Watch out: the admin console gains a third writable surface. The console's read-only / editable split is amended, and the console router's no-write test still holds, because the write lives elsewhere.
- Watch out: `store=false` is a proxy-enforced data-retention property. If OpenAI renames or defaults it differently, requests will be refused rather than stored, which is the intended direction.

## Amendment 2026-10-17: unknown models are refused, not priced at the ceiling

§Decision 4's "unknown models are charged at the most expensive row" is superseded by ADR 0014 §Amendment 2026-10-17 point 2. A model with no row (a dated snapshot of a priced model still counts as priced: `-YYYY-MM-DD` for this dialect, `-YYYYMMDD` for the anthropic one, which reports an alias as the snapshot it resolved to) is refused at the proxy. For this dialect the ceiling row was $1.75/$14 per MTok, while the same endpoint serves models priced near $15/$120.

## Amendment 2026-10-28: the top-level request surface is an allowlist in both dialects

§Decision 3's "Refused fields" row is replaced by the two rows above. The rule it replaced named the fields the provider was known to act on (`mcp_servers`, `container`; and four state fields on the OpenAI side) and forwarded everything else, so a request field the provider ships next, or one that changes the price (`inference_geo`, which the CLI's own cost code prices at 1.1x for `us`; `service_tier`; `speed`), went through. **Decision: each dialect forwards exactly the top-level fields its pinned engine was measured to send, and an unknown field is a recorded policy refusal before the forward** (`request field '<name>' is not allowed`, even when null or empty; spends nothing; the build fails closed naming the field). The sets are measured, not recalled: the real runner in the pinned image against a recording server (`tests/test_agent_tool_capture_integration.py`, `tests/test_openai_sdk_wire_integration.py` re-derive them, and an SDK bump that adds a field is a red test before it is a refused build). The one measured exception is outside the build: the CLI's DEFAULT tool set (not ours) issues a nested provider-side search request that carries `tool_choice`, and that request is refused for the field and for its server tool. The OpenAI dialect's `tool_choice` check was removed with it: the runner never sends one, so an edit that admits it must also validate its `type`. The refusal is a 403 in the engine's own envelope like every other policy refusal, not a bare 400, so it is recorded and the executor can name it.
