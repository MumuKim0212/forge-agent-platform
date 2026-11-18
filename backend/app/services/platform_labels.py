"""The labels every container and network the platform creates carries (ADR 0037
§Decision 1-2). The reconciler (`services/reconciler.py`) finds platform objects by them and
by nothing else: never by name, never by prefix.

Three things are stamped on an object:

* `forge.instance` -- the id of THIS installation (see `services/instance_id.py` for where it
  is stored). The reconciler acts only on objects whose instance is its own, so a second
  checkout on the same Docker daemon, or a test run, can neither reap nor be reaped by it.
* `forge.role` -- one of the closed set in `Role`. A role the reconciler does not know is an
  object it does not touch.
* the ids that tie the object to a database row: `forge.build_id`, `forge.deployment_id`,
  `forge.project_id` (the keys the L4 and preview containers already carried).

**Every create site goes through `platform_labels`.** `tests/test_platform_labels.py` reads
`app/` as source and fails for any `containers.run` / `containers.create` / `networks.create`
whose labels do not come from it, and pins the role each site passes -- so an object the
reconciler cannot see cannot be added by accident.

**The instance id is process state, set once.** The worker binds it from the database at start
(`instance_id.bind_instance_id`); the test suite pins its own per run. Reading it unbound
raises: creating an unlabelled container would hide it from the reconciler for good, and a
create that fails loudly is the better failure.
"""

from __future__ import annotations

LABEL_INSTANCE = "forge.instance"
LABEL_ROLE = "forge.role"
LABEL_BUILD_ID = "forge.build_id"
LABEL_DEPLOYMENT_ID = "forge.deployment_id"
LABEL_PROJECT_ID = "forge.project_id"


class Role:
    """The closed set of roles (ADR 0037 §Decision 1)."""

    SANDBOX = "sandbox"
    LLM_PROXY = "llm-proxy"
    BUILD_NETWORK = "build-network"
    PREVIEW = "preview"
    PREVIEW_REDIS = "preview-redis"
    PREVIEW_NETWORK = "preview-network"
    DEPLOY = "deploy"
    DEPLOY_NETWORK = "deploy-network"
    SERVICE_REDIS = "service-redis"
    # an IMAGE role (the only one). The reconciler lists containers and
    # networks and never an image; `services/image_retention.py` is what selects by it.
    BUILD_IMAGE = "build-image"

    ALL = (
        SANDBOX,
        LLM_PROXY,
        BUILD_NETWORK,
        PREVIEW,
        PREVIEW_REDIS,
        PREVIEW_NETWORK,
        DEPLOY,
        DEPLOY_NETWORK,
        SERVICE_REDIS,
        BUILD_IMAGE,
    )


class InstanceIdUnsetError(RuntimeError):
    """A platform object was about to be labelled before the instance id was bound."""


_instance_id: str | None = None


def set_instance_id(value: str) -> None:
    """Binds this process's instance id. Called once at worker start
    (`instance_id.bind_instance_id`) and by the test suite's own pin."""
    global _instance_id
    if not isinstance(value, str) or not value.strip():
        raise ValueError("the instance id must be a non-empty string")
    _instance_id = value


def clear_instance_id() -> None:
    """Unbinds it (tests only)."""
    global _instance_id
    _instance_id = None


def instance_id() -> str:
    """This process's instance id. Raises `InstanceIdUnsetError` when nothing bound one."""
    if _instance_id is None:
        raise InstanceIdUnsetError(
            "the platform instance id is not bound in this process, so no container or network "
            "can be labelled (and an unlabelled one is invisible to the reconciler); the worker "
            "binds it from the database at start"
        )
    return _instance_id


def platform_labels(
    role: str,
    *,
    build_id: str | None = None,
    deployment_id: str | None = None,
    project_id: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """The label set for one platform object. `extra` carries the labels an object has always
    had (`forge.deploy`, `forge.network`, `forge.data_service`, ...); the platform keys win over
    it, so an `extra` can never change an object's instance or role. A role outside `Role.ALL`
    raises: the set is closed."""
    if role not in Role.ALL:
        raise ValueError(f"unknown platform role {role!r}")
    labels: dict[str, str] = dict(extra or {})
    labels[LABEL_INSTANCE] = instance_id()
    labels[LABEL_ROLE] = role
    if build_id is not None:
        labels[LABEL_BUILD_ID] = build_id
    if deployment_id is not None:
        labels[LABEL_DEPLOYMENT_ID] = deployment_id
    if project_id is not None:
        labels[LABEL_PROJECT_ID] = project_id
    return labels
