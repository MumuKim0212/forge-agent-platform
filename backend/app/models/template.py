from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, IdMixin


class Template(IdMixin, CreatedAtMixin, Base):
    __tablename__ = "templates"

    name: Mapped[str]
    stack: Mapped[str]
    dockerfile_ref: Mapped[str]
    deploy_config_ref: Mapped[str]
    policy_profile: Mapped[str] = mapped_column(default="default")
    version: Mapped[str] = mapped_column(default="0.1.0")
    enabled: Mapped[bool] = mapped_column(default=True)
