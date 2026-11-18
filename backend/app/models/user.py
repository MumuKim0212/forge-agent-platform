from sqlalchemy import Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, IdMixin
from app.services.account_status import AccountStatus
from app.services.roles import Role


class Team(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "teams"

    name: Mapped[str] = mapped_column(unique=True)

    members: Mapped[list["User"]] = relationship(back_populates="team")


class User(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "users"

    name: Mapped[str]
    email: Mapped[str] = mapped_column(unique=True)
    # ADR 0018 §3: the OIDC `sub`
    # is the identity key — lookup is by this column, never by email (email
    # is mutable and reassignable in some directories). Nullable because
    # dev-mode seeded users have no IdP identity at all.
    oidc_sub: Mapped[str | None] = mapped_column(unique=True, nullable=True)
    # the value set and its ranking live in
    # `services/roles.py` — the one declaration site — so the default here is
    # `Role.REQUESTER` rather than a re-spelled literal. Still a plain
    # non-null string column: no enum, no migration, no rename.
    role: Mapped[str] = mapped_column(default=Role.REQUESTER)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    # this user's monthly
    # cost limit override. **`NULL` means "use `QUOTA_MONTHLY_USD_DEFAULT`",
    # not unlimited** — the effective limit is resolved per read in
    # `services/usage.effective_limit`, never cached. Admin-writable
    # (`PATCH /usage/quotas/users/{id}`), effective immediately, no restart.
    monthly_quota_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # ADR 0022 §3: the
    # bcrypt hash of this user's `local`-mode password. Nullable — OIDC-
    # provisioned users and dev-seeded rows never have one, and
    # `password_hash IS NULL` can never authenticate (services/passwords.py).
    # **Never** appears in any response schema — the same handling class as
    # `Project.service_env_json`.
    password_hash: Mapped[str | None] = mapped_column(nullable=True)
    # ADR 0022 §6: free-text
    # evidence for the approving admin, **never** an authorization input
    # (roles remain the only authorization axis). Nullable — only
    # local signup requests populate it.
    department: Mapped[str | None] = mapped_column(nullable=True)
    # ADR 0022 §6-7: the
    # single declaration site for these values is `services/account_status.
    # AccountStatus` — see that module for the transition table. Default
    # `ACTIVE` (not `PENDING`) so every row created outside the local-signup
    # path — OIDC JIT provisioning, the dev seed, the local bootstrap admin —
    # is active without every one of those call sites having to set it
    # explicitly; only `services/signup.request_signup` overrides it to
    # `PENDING`. Same "plain non-null string column, Python-side default"
    # shape as `role` above — no enum, no migration
    # surprise later.
    status: Mapped[str] = mapped_column(default=AccountStatus.ACTIVE)

    team: Mapped[Team | None] = relationship(back_populates="members")
