"""the platform instance id this test run uses (ADR 0037 §Decision 2).

Every container and network the code under test creates carries `forge.instance=<this>`,
and a `Reconciler` built for a test acts on this value only. It is generated once per pytest
process, is never a value the platform could store for a real installation (a stored id is a
bare uuid hex), and so can neither match the dev host's live objects (`forge-deploy-...`,
`forge-service-postgres` carry no instance label at all) nor be matched by a running dev
worker's reconciler. `tests/conftest.py` pins it for the whole session and again for every
test (a test that binds another id, `worker.main` does, is undone afterwards).

Tests read it from here, not from `platform_labels.instance_id()`: the expected side of a
label assertion must not come from the code under test.
"""

import uuid

TEST_INSTANCE_ID = f"forge-test-{uuid.uuid4().hex[:12]}"
