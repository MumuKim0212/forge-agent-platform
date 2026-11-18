"""the deployment failure codes the backend can record are exactly the
codes the frontend has a Korean label for.

Nothing else ties the two sides together: the frontend's own test spells the
list out as a literal (`ALL_DEPLOYMENT_CODES`), so adding a ninth code on either
side is invisible to the other side's suite. This reads that literal from the
frontend test file as text and compares it to the enum's members.
"""

import re
from pathlib import Path

from app.models.deployment import DeploymentFailureReason

FRONTEND_TEST = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "components"
    / "deploymentFailure.test.ts"
)


def _frontend_literal_codes() -> list[str]:
    source = FRONTEND_TEST.read_text(encoding="utf-8")
    match = re.search(r"const ALL_DEPLOYMENT_CODES = \[(.*?)\];", source, re.DOTALL)
    assert match is not None, "ALL_DEPLOYMENT_CODES literal not found in the frontend test"
    return re.findall(r'"([A-Z_]+)"', match.group(1))


def _backend_codes() -> list[str]:
    return [
        value
        for name, value in vars(DeploymentFailureReason).items()
        if not name.startswith("_") and isinstance(value, str)
    ]


def test_frontend_literal_is_read_and_non_trivial() -> None:
    codes = _frontend_literal_codes()

    # A parse that quietly returned [] would make the parity test below vacuous.
    assert len(codes) == len(set(codes)) >= 8, codes


def test_backend_enum_members_equal_the_frontend_literal() -> None:
    assert sorted(_backend_codes()) == sorted(_frontend_literal_codes())


FRONTEND_MODULE = FRONTEND_TEST.with_name("deploymentFailure.ts")


def _frontend_map_keys(name: str) -> list[str]:
    """The keys of one `Record<string, string>` object literal in `deploymentFailure.ts`, as text."""
    source = FRONTEND_MODULE.read_text(encoding="utf-8")
    match = re.search(rf"export const {name}: Record<string, string> = \{{(.*?)\n\}};", source, re.DOTALL)
    assert match is not None, f"{name} not found in deploymentFailure.ts"
    return re.findall(r"^\s{2}([A-Z_]+):", match.group(1), re.MULTILINE)


def test_every_backend_code_has_a_label_and_a_requester_next_step_in_the_frontend_module() -> None:
    """a code added to `DeploymentFailureReason` fails here until the frontend module words
    it twice, as a label and as a next step."""
    backend = sorted(_backend_codes())

    assert sorted(_frontend_map_keys("DEPLOYMENT_FAILURE_REASON_LABELS")) == backend
    assert sorted(_frontend_map_keys("DEPLOYMENT_FAILURE_NEXT_STEPS")) == backend
