from app.models.approval import ApprovalRequest
from app.models.audit import AuditLog
from app.models.build import Build
from app.models.build_trace import BuildTrace
from app.models.change_request import ChangeRequest
from app.models.deployment import Deployment
from app.models.engine_selection import EngineSelection
from app.models.eval_run import EvalCase, EvalRun
from app.models.host_disk_check import HostDiskCheck
from app.models.notification import NotificationChannelStatus, NotificationState
from app.models.platform_instance import PlatformInstance
from app.models.policy_verdict import PolicyCheckResult, PolicyVerdict
from app.models.project import Project
from app.models.service_checker_status import ServiceCheckerStatus
from app.models.service_health_check import ServiceHealthCheck
from app.models.template import Template
from app.models.traffic_signal import TrafficSignalState
from app.models.user import Team, User
from app.models.worker_lease import WorkerLease

# Every model module must be imported here: SQLAlchemy resolves a
# relationship's target class by name when mappers configure, so a module
# that nothing imported (a bare `python -m app.seed`) fails on first use.
# `tests/test_models_package_registers_all_mappers.py` pins it.
__all__ = [
    "ApprovalRequest",
    "AuditLog",
    "Build",
    "BuildTrace",
    "ChangeRequest",
    "Deployment",
    "EngineSelection",
    "EvalCase",
    "EvalRun",
    "HostDiskCheck",
    "NotificationChannelStatus",
    "NotificationState",
    "PlatformInstance",
    "PolicyCheckResult",
    "PolicyVerdict",
    "Project",
    "ServiceCheckerStatus",
    "ServiceHealthCheck",
    "Template",
    "Team",
    "TrafficSignalState",
    "User",
    "WorkerLease",
]
