from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.models.template import Template
from app.models.user import Team, User

GENERIC_WEBTOOL_TEMPLATE_NAME = "범용 웹툴"

SEED_TEAM_NAME = "Operations Team"

SEED_USERS = [
    {"name": "김민지", "email": "minji.kim@example.com", "role": "requester"},
    {"name": "이도현", "email": "dohyun.lee@example.com", "role": "approver"},
    {"name": "박서연", "email": "seoyeon.park@example.com", "role": "admin"},
]


def seed(db: Session, *, is_prod: bool | None = None) -> None:
    """Idempotent: safe to call on every startup. Never deletes/overwrites
    existing rows — only inserts what's missing.

    prod seeds the template only, never the dev team/users.
    `is_prod` defaults to the process-wide runtime mode; tests pass it
    explicitly to exercise both branches without mutating global settings."""
    if is_prod is None:
        is_prod = settings.is_prod

    template = db.query(Template).filter_by(name=GENERIC_WEBTOOL_TEMPLATE_NAME).first()
    if template is None:
        template = Template(
            name=GENERIC_WEBTOOL_TEMPLATE_NAME,
            stack="python-fastapi-postgres-redis",
            dockerfile_ref="templates/generic-webtool/Dockerfile",
            deploy_config_ref="templates/generic-webtool/compose.yaml",
            policy_profile="default",
            version="0.1.0",
            enabled=True,
        )
        db.add(template)

    if is_prod:
        db.commit()
        return

    team = db.query(Team).filter_by(name=SEED_TEAM_NAME).first()
    if team is None:
        team = Team(name=SEED_TEAM_NAME)
        db.add(team)
        db.flush()

    for seed_user in SEED_USERS:
        existing = db.query(User).filter_by(email=seed_user["email"]).first()
        if existing is None:
            db.add(User(team_id=team.id, **seed_user))

    db.commit()


def run() -> None:
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()


if __name__ == "__main__":
    run()
