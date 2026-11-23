# 0048. Approval requests are read by approvers, and by the people a request belongs to

- Status: accepted
- Date: 2026-11-23

## Context

The platform's role/route permission matrix (not published) let every authenticated user list and read every `ApprovalRequest` ("List/read projects, timeline, builds, deployments, approvals: authenticated"). The approval console was also in the navigation of every signed-in user. This was recorded as an open decision, to be stated before the first real tenant.

Two things make it worth deciding now:

- The navigation and the permission model disagree. The admin console link is hidden from non-admins through the same predicate the page guards itself with; the approval console link was shown to everyone, and only the approve and reject buttons were gated.
- The approval detail carries more than the timeline does: every check's findings, the measured facts, the agent's summary and the requester's request text, for any project of any team.

What a requester needs does not require the console. A requester sees the status of their own request in the project timeline, and that entry is not filtered by role.

## Decision

1. **The approval list** (`GET /approvals`, with or without its `status` filter) requires `approver` or above. Anyone else gets a refusal, the same shape as approve/reject's role refusal.
2. **One approval's detail** (`GET /approvals/{id}`) requires `approver` or above, **or** being the request's requester (`ApprovalRequest.requester_id`), **or** being the owner of the request's project (`Project.owner_id`). This is the shape of the build-trace read rule (the project owner, or approver and above; backend predicate `may_read_build_trace`), and it is one named predicate on the backend (`may_read_approval_request`), never an inline comparison. An unknown id is still a 404 for everyone: ids are not guessable and their existence is not a secret.
3. **The frontend follows with one named predicate** (`canOpenApprovalConsole`, the approver rung) for "may open the approval console". The navigation shows the entry only when it holds, and the `/approvals` page refuses on screen when it does not, the way `/admin` does. A hidden link and an open page cannot disagree, because both call it. Navigation visibility stays presentation only; the backend is authoritative.
4. **Unchanged:** who may approve or reject, the self-approval ban (no admin exception), `approver` being a global role, the build-trace rule, and the requester's own status in the project timeline.

## Alternatives considered

- **Hide the menu entry only.** Cosmetic: the list and every detail stay readable by direct request. It would make the navigation and the model agree while leaving the exposure, which is worse than disagreeing in the open. Rejected.
- **A filtered list for a requester** (their own requests only). It adds a query shape and a screen nobody needs, since the timeline already carries a requester's status. Rejected; it can be added if a requester ever needs a "my requests" view.
- **Keep it open for transparency.** Reasonable for a single small team. Rejected because the platform is meant to hold several teams' requests, and the detail includes the agent's summary and every finding.

## Consequences

- **The project timeline is not changed by this decision.** It still shows the approval entry (status and counts), the flagged check names and the agent-authored summary of any project to every authenticated user, because "read projects, timeline" stays open to every authenticated user in the platform's permission matrix. This decision closes the approval list and detail, not that surface. Whether the timeline should be scoped to a project's team or owner is a separate decision, to be taken before a second team uses the platform.
- Tests and callers that read the approval list as a plain requester must use an approver. A requester who made a request, or owns its project, still reads its detail.
- A requester who is also a project owner can read the detail of their own request by direct request, though no screen links to it from the timeline.
