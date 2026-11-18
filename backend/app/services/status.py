class ProjectStatus:
    """Project lifecycle states, in pipeline order.

    DRAFT is reserved: nothing creates a draft today. SUBMITTED is where a
    project starts; BUILD_PENDING -> PREVIEW_READY -> AWAITING_APPROVAL -> ACTIVE
    are reached only through `services/project_status.advance`, forward
    only. ARCHIVED is set by the archive action and cleared by un-archive,
    never by a forward transition."""

    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    BUILD_PENDING = "BUILD_PENDING"
    PREVIEW_READY = "PREVIEW_READY"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ProjectKind:
    """What a project is for. `NORMAL` is someone's service; `EVAL` is
    the throwaway project an eval case builds in. An eval project is hidden from the default
    registry list, excluded from the zombie sweep, and refuses deploy confirmation -- it is
    still a project in every other respect (its builds, trace and cost are real)."""

    NORMAL = "normal"
    EVAL = "eval"


class BuildStatus:
    """Build/Job lifecycle states. The API creates builds as `QUEUED`
    when the sandbox executor is active (`use_sandbox_executor` + a real
    key — routers/projects.py `_initial_build_status`), otherwise
    `NOT_IMPLEMENTED` exactly as before (ADR 0001: no fake states);
    `QUEUED` -> `RUNNING` -> `SUCCEEDED`/`FAILED` is driven by the worker.
    """

    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    SUCCEEDED = "SUCCEEDED"


class BuildFailureReason:
    """Reason codes for a FAILED build (the failure taxonomy).
    Every reason is reachable: TIMEOUT/RESOURCE_LIMIT/
    AGENT_ERROR/HARVEST_ERROR/INFRA_ERROR from the sandbox path,
    BUDGET_EXCEEDED/ENGINE_UNREACHABLE from the LLM proxy path
    (sandbox_build_executor.py, ADR 0014)."""

    TIMEOUT = "TIMEOUT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    ENGINE_UNREACHABLE = "ENGINE_UNREACHABLE"
    AGENT_ERROR = "AGENT_ERROR"
    HARVEST_ERROR = "HARVEST_ERROR"
    INFRA_ERROR = "INFRA_ERROR"
    # the build's output has no file
    # the template's service starts from (`services/service_entrypoint.py`).
    MISSING_ENTRYPOINT = "MISSING_ENTRYPOINT"


class PreviewStartFailure:
    """why a build's preview did not start. Platform-authored codes only, enumerated where
    `PreviewManager` constructs its errors (never exception text).
    The build still ends SUCCEEDED: a preview is a convenience, not a gate."""

    NO_IMAGE = "NO_IMAGE"  # the build has no image_ref to run
    IMAGE_MISSING = "IMAGE_MISSING"  # an image the preview needs (its own or a data service's) is absent
    NETWORK_FAILED = "NETWORK_FAILED"  # the preview's private network could not be created
    DATA_SERVICES_FAILED = "DATA_SERVICES_FAILED"  # throwaway database/Redis failed (not a missing image)
    CONTAINER_START_FAILED = "CONTAINER_START_FAILED"  # Docker refused the preview container
    SIGNING_SECRET_MISSING = "SIGNING_SECRET_MISSING"  # no usable preview_signing_secret (config fault)
    UNEXPECTED = "UNEXPECTED"  # anything else; the worker log has the class and text
