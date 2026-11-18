from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, JSON, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, IdMixin
from app.services.status import ProjectKind, ProjectStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "projects"

    name: Mapped[str]
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"))
    template_id: Mapped[str] = mapped_column(ForeignKey("templates.id"))
    intent_spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(default=ProjectStatus.SUBMITTED)
    # Reserved for L3/L4 slices — left nullable so populating them later is
    # an UPDATE, not a migration.
    repo_url: Mapped[str | None] = mapped_column(nullable=True)
    container_id: Mapped[str | None] = mapped_column(nullable=True)
    # the repo ref of the last build
    # merged via a successful confirm. Modify-type builds base on this once
    # set so repo state and deployed
    # state stay aligned on the happy path. Nullable: unset until the
    # project's first successful confirm.
    confirmed_ref: Mapped[str | None] = mapped_column(nullable=True)
    # the project's service env —
    # issued once at first deploy (service identity + app secret key),
    # stable across redeploys/rollbacks, injected as container env at
    # create. SECRET-BEARING: never serialized into any API schema
    # (`ProjectOut` deliberately omits it), never logged, never put in an
    # audit entry's metadata).
    #
    # this column holds Fernet CIPHERTEXT, not
    # plaintext JSON — a raw `SELECT` returns bytes an operator cannot read
    # without `SERVICE_ENV_ENCRYPTION_KEY`. `Text`, not `JSON`, precisely so
    # nothing can round-trip it through a JSON codec by accident; the name
    # is unchanged (a rename would touch every existing reference to this
    # column for no behavioural gain). The only production reader/writer
    # for ISSUING or READING it is `services.deploy.ensure_service_env`,
    # via `services.service_env_crypto.encrypt_service_env`/
    # `decrypt_service_env`. The one other legitimate
    # writer: `routers/projects.py`'s admin-only deprovision action sets
    # this back to `None` once the database it describes has been dropped
    # — a retained `DATABASE_URL` pointing at nothing would be worse than
    # none. Nothing else may read or write this column directly.
    service_env_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    # this project's
    # monthly cost limit override. **`NULL` means "use
    # `QUOTA_MONTHLY_USD_DEFAULT`", not unlimited** — resolved per read in
    # `services/usage.effective_limit`, never cached. Admin-writable
    # (`PATCH /usage/quotas/projects/{id}`), effective immediately. A column
    # here rather than a polymorphic `quotas` table: with exactly two subject
    # kinds a column keeps the FK and buys nothing away.
    monthly_quota_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # `normal` (someone's service) or `eval` (the throwaway project one
    # eval case builds in). Written once, when the project is created, by the eval runner;
    # nothing changes it afterwards. Read by the registry's default filter, the zombie sweep's
    # candidate query and deploy confirmation. A plain string like every status-like column.
    kind: Mapped[str] = mapped_column(default=ProjectKind.NORMAL)
