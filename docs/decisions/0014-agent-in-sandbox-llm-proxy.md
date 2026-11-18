# 0014. Agent runs inside the sandbox; credentials stay in L2 (LLM egress proxy)

- Status: accepted
- Date: 2026-07-06

## Context

ADR 0003 (default-deny egress, disposable sandbox) and the choice of the Claude Agent SDK as the agent engine collide on one question: **where does the agent loop run?** The SDK's tools (file edits, shell) execute where the SDK process runs, so the natural place is inside the L3 sandbox — but then the sandbox would need the Anthropic API key and open egress, putting a real credential in the same container as unvetted generated code. Running the loop in L2 instead would keep the key safe but requires proxying every tool call into the container, fighting the SDK's execution model.

## Decision

The agent (SDK process) runs **inside the L3 sandbox** — its native mode; tools act directly on the sandbox workspace. The credential problem is solved by keeping the key out of L3 entirely:

- An **LLM egress proxy** runs in L2. The sandbox gets `ANTHROPIC_BASE_URL` pointed at the proxy and a **per-build ephemeral proxy token** (random, issued at sandbox start, revoked at build end). The real Anthropic API key exists only in the proxy's environment.
- The proxy: injects the real key, enforces a **per-build token/cost budget** (build fails with reason `BUDGET_EXCEEDED` when spent), records usage into the `Build` cost fields (feeds cost metering later), and rejects tokens of non-active builds.
- Sandbox egress allowlist (ADR 0003 invariant 3 stays default-deny): the LLM proxy and package registries (PyPI/npm) — nothing else. No git server, no platform API, no prod services.

## Alternatives considered

- **Raw API key in the sandbox env:** simplest; unacceptable — generated code can read the key, and it outlives the build if exfiltrated (e.g. committed into the diff).
- **Agent loop in L2, tools proxied via docker-exec into the sandbox:** best credential story, but replaces the SDK's built-in tool execution with a custom remote-execution layer — significant moving parts for a first version. Revisit if the engine is ever swapped (the thin engine adapter is the seam).
- **Short-lived scoped Anthropic keys per build:** still a real credential inside L3, still needs direct Anthropic egress, and no central budget/cost capture point.

## Consequences

- Gain: SDK used as designed; no durable credential ever enters L3; budgets and cost metering enforced at one chokepoint; the proxy token is worthless outside the proxy and dies with the build.
- Give up: the proxy is new L2 infrastructure on the build critical path (proxy down → builds fail fast with `ENGINE_UNREACHABLE`).
- Watch out: package registries remain a theoretical exfil channel — acceptable for now (needed for installs); narrow later via a registry mirror/proxy and the policy gate. Proxy tokens must be unusable after build end (revocation, not expiry alone).

## Amendment 2026-10-01 — what the proxy meters, and the engine is never a source

Written while scoping the work to fix metering. §Decision says the proxy enforces the per-build budget and records usage into the `Build` cost fields. Against the real engine it did neither:

- `_account_usage` parsed only a JSON body. The pinned CLI streams (SSE), so every real call was recorded as 0 tokens (measured).
- The pre-call budget check reads that counter, so `BUDGET_EXCEEDED` was unreachable mid-build.
- Cache tokens were never read, and the CLI's model (`claude-opus-5-5`) was not in the price table.
- The executor then filled `builds.cost_*` from the engine's own `result.json` whenever the proxy reported 0, which against the real engine was always. That file is written inside L3, so **the figures behind the usage quotas were attacker-controllable** in both directions.

Every test passed because the stub engines report usage and the JSON stub upstream is accounted. The fixtures exercised a different system.

**Decisions:**

1. **The proxy is the only source of `builds.cost_tokens` / `cost_usd`.** The engine's self-reported usage is never written to a cost field. It may be logged beside the proxy's figure as a diagnostic, labelled as engine-reported. A build whose proxy usage cannot be read back at build end gets `NULL` cost fields (not measured) and fails closed. A build whose spend cannot be shown to be within budget does not publish.
2. **The proxy meters streamed and non-streamed responses alike.**
   - For SSE, usage is `message_start.message.usage`, superseded field by field by each later `message_delta.usage` (the API reports cumulative counts).
   - The model is taken from `message_start.message.model`.
3. **Priced components:**
   - `input_tokens` at the input rate;
   - `cache_creation_input_tokens` at 1.25× input for the 5-minute TTL and 2× for the 1-hour TTL, using the `cache_creation` breakdown when present and **2× for all of it when absent**;
   - `cache_read_input_tokens` at the model's own cache-read rate, which is not a uniform multiplier across models;
   - `output_tokens` at the output rate.
   - `usage.speed == "fast"` doubles every rate.
4. **The token counter is `input + cache_creation + output`.** Cache reads are priced into `cost_usd` but are not counted as tokens. They re-read tokens already counted, so counting them would make the token cap grow with the square of the turn count rather than measure work. The cost budget is what bounds spend.
5. **The price table is code, with a conservative ceiling.**
   - The rows are the list rates for every model the platform can be sent. Unknown models are charged at the most expensive row, never a cheaper default.
   - The model the pinned CLI actually sends must have its own row. The engine-up capture pins this, so an SDK/CLI bump that changes the model is caught.
6. **An unmeterable success fails closed.**
   - This covers a 2xx Messages response from which no usage can be read: not JSON with `usage`, and not SSE with a `message_start` usage.
   - The proxy marks the token unmetered and refuses every later request, with a distinct detail.
   - The executor fails the build with `BUDGET_EXCEEDED`, with a message saying usage could not be metered. When the budget can no longer be enforced, that counts as exhausted.
7. **Enforcement stays pre-call.** One call can overshoot the budget by at most that call's own cost. Cutting a stream mid-flight would leave the engine with a broken response for no spend saved, since the provider bills what it generated.

**Watch out:** list prices change. The table cites its source and date, and a price change is a code change. A provider-side error that arrives as an HTTP error rather than a 2xx stream is not accounted. Such a response may have been billed partially; this is accepted as rare and recorded here rather than guessed at.

## Amendment 2026-10-11 — one dialect per engine (ADR 0035)

The proxy's core (registry, budget, fail-closed metering, admin API, trace bounds) is engine-neutral. The Messages-API specifics become the `anthropic` dialect, unchanged. An `openai` dialect (Responses API) is added, with its own allowlist, auth header, usage parse and price table. A proxy container runs exactly one dialect and holds exactly one key. Every decision in the 2026-10-01 amendment applies to both dialects.

## Amendment 2026-10-17: the budget bound holds under concurrency, unknown models are refused, and headers are allowlisted

A code review on 2026-10-16 confirmed three ways the 2026-10-01 amendment's guarantees did not hold against an agent that has a shell and its proxy token:

1. **Point 7 ("overshoot by at most that call's own cost") assumed one call at a time.** The handler checks the budget, awaits upstream, and only then records usage. N concurrent requests all pass the check against the same total, so the overshoot is N calls, and the httpx pool queues up to 100 already-checked requests. **Decision: calls are serialised per token.** One upstream call is in flight per build; the next request waits for it, and the budget check runs only after it has acquired the slot. This makes point 7 true as written. Rejected: reserving a worst-case cost per in-flight call. It is more parallel, but it needs a per-dialect worst-case estimate from `max_tokens` and input size, which is one more number to get wrong, at a scale (one build, one agent loop) where parallel calls buy little.
2. **Point 5's "unknown models are charged at the most expensive row" is not conservative.** Providers sell models priced above any row in the table, so the ceiling undercounts them (by about 8× for an OpenAI `-pro` model). **Decision: a request whose model has no price row is refused**, as a policy refusal like any other, recorded and never silent. A dated snapshot of a priced model is still priced at its base row (ADR 0035 §4). The table is therefore also the model allowlist, which is what "the rows are the list rates for every model the platform can be sent" already claimed.
3. **Headers passed by denylist in both directions.** Every L3 header except auth/host/length reached upstream beside the real key, including `OpenAI-Organization`/`OpenAI-Project` (billing another project) and pricing-relevant `anthropic-beta` values. Every upstream header came back to L3. **Decision: per-dialect header allowlists in both directions.** The inbound list holds exactly what the pinned SDKs send (measured from the pinned versions, not guessed). An `anthropic-beta` value is forwarded only if it is on a measured list and does not change pricing in a way the table does not model.

Recorded at implementation:
- **A per-token queue cap.** Serialising calls makes later requests wait, and a waiting request has already buffered its body, up to the 16 MiB bound then in force. Without a cap, a build's own code could hold requests × 16 MiB in the proxy's memory. At most 8 requests per token may be live; the next is refused with 429 before its body is read.
- **An unmeasured `anthropic-beta` value refuses the request; it is not stripped.** Stripping would silently change what the engine asked for and could change its behaviour in ways nobody chose. A refusal is recorded and visible, and adding a newly measured value is a one-line change.
- **Disallowed OpenAI routing headers are dropped, not refused.** `OpenAI-Organization`/`OpenAI-Project` are not on the inbound list, so the request goes out without them and bills the key's default project, which is the intended one.

Two smaller rules travel with these:
- **Every nested content list is policy-checked.** A `document` source of type `content` carries its own blocks, and a URL image inside it is the same outbound channel ADR 0029 closes at the top level.
- **The engine's own result file can never suppress the proxy's record.** A malformed L3-written `result.json` must not stop the proxy's usage, refusals and budget outcome from being read and recorded; L3 cannot hide what L2 measured.

## Amendment 2026-10-24: a cancelled call fails closed, and usage is read only after the token is drained

- **A forwarded call that is cancelled leaves the token unmetered.** If a `CancelledError` escapes after the forward began, the call's spend went unrecorded, so the token is marked unmetered before its slot is released. The build ends `BUDGET_EXCEEDED`, like an unmeterable response. A cancel while still waiting for the slot marks nothing, because nothing was sent.
- **Usage is read only after the token is drained.** The executor closes the token through the proxy's control listener. A `closed` token refuses new and queued requests like a revoked one, but its usage and trace stay readable. The executor then waits, bounded, until the token's slot is free and its in-flight count is 0, and only then reads usage. Admission (look-up, closed check, cap, count) is one step under the registry lock, because the drain reads the count from another thread.
- **A drain that fails or runs out is a read-back failure**: both cost fields NULL and the build fails, and no short figure is ever stored. The bound must cover the proxy's own upstream timeout (120 s): a call whose caller died can stay in flight that long. The first implementation used a 30 s bound, which failed closed but turned a killed-mid-call `TIMEOUT` into `INFRA_ERROR`. **Follow-up:** the proxy's upstream timeout is the named constant `UPSTREAM_TIMEOUT_SECONDS` (120 s). The executor drains in counted 30 s steps up to that plus 30 s, so the drain is bounded whatever the proxy answers. A drain that still runs out keeps the build's original reason, with cost NULL. *Reasoned, not measured:* httpx's timeout is per phase and restarts on every chunk, so a call that keeps streaming can outlast the total. That path is fail-closed and keeps its own reason.
- **The proxy image and the worker change together.** A worker on this code against an image that predates the drain route fails every build's usage read closed (the drain route is missing). **Follow-up:** the image advertises its capabilities with a label (`forge.proxy.capabilities="drain"`), not a health field. One `image inspect` creates no container and handles no key, so the same read serves the worker's start banner and every build; a health field exists only on a running proxy. The cost: a label is stamped at build time, not a measurement, and the real-container drain tests are what prove it. A build against an image without the capability is refused before the agent runs (`INFRA_ERROR`, `no agent ran: …` naming the image and the rebuild command), so no money is spent on a run whose cost could never be recorded. An unlabelled image, even one that can drain, is refused as unknown: rebuild when you pull.

Not decided here: a transport error after the request was sent (a read timeout or reset the provider may still bill) is the same unrecorded-spend family, but failing the token on every one would turn network blips into failed builds. It is deferred, with a trigger for revisiting.

## Amendment 2026-10-28: every post-send failure is unmetered, and the body cap is set by what a parse can hold

- **Anything but a measured response leaves the token unmetered.** The previous amendment marked a token unmetered when a forwarded call was cancelled. Any other exception that leaves the forward does the same (a `RuntimeError` from the transport, an error while the response body is read), as does a usage that was read but could not be recorded. Enumerating exception types was the failure; the boundary is now "a measured response, an `httpx.HTTPError`, or an unmetered token". What it cannot tell apart is a failure before the first byte left from one after, and both mark: the fail-closed side. The `httpx.HTTPError` family is unchanged and is still the open decision above.
- **The request body cap is 4 MiB, down from 16 MiB.** The 16 MiB figure bounded what the proxy *reads*, not what it *holds*: `json.loads` expands a hostile body of small containers 25 to 33 times (measured on the image's Python 3.14.8; worst shape `[{"":{}},...]`, 33.2x), so one authenticated 16 MiB request peaked at 612 MiB and killed the 512 MiB container (`OOMKilled`, reproduced against the real container), taking the registry with it. The parse is synchronous, so parses never overlap; the other memory is the bodies waiting on the slot (the 8-request cap above). Worst case = process (~50 MiB) + (8 + 33 + 2) x the cap: ~222 MiB at 4 MiB (observed 220 MiB peak in the real container with a hostile body and seven others queued), ~740 MiB at 16 MiB. 4 MiB is more than three times the largest legitimate conversation the token budget allows (~1.2 MB of JSON) and leaves room for a couple of base64 images. The per-token queue cap's "requests x 16 MiB" above is now requests x 4 MiB. Rejected: a structural pre-parse bound (counting containers or commas), which is either loose (it must admit comma-heavy legitimate text) or a second parser to keep in step with the first; and a lower queue cap, which would leave a single 16 MiB parse over the container.
