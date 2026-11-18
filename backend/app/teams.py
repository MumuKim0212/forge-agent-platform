"""Operator command: create and list teams.

    python -m app.teams create "<name>"
    python -m app.teams list

Every project needs an owning team, prod seeding inserts only the template
 and the console is read-only, so on a fresh prod install this is the
only way to get a first team. Run on the host as the service user from
`backend/`, against the configured platform database.

- `create` is idempotent by exact (stripped) name: a second run prints the existing
  id and inserts nothing. It writes one audit entry per row actually inserted:
  `actor_id=None`, `action="team.created"`, `target="team:<id>"`,
  `metadata={"actor": "operator command", "name": <name>}` (no user row stands for
  the operator; same actor_id=None convention as the worker's entries).
- Both refuse when the schema is not at the Alembic head, with the same
  message the server uses.
- A database error prints the exception class only, never its message (a driver
  message can carry the DSN).

Exit status: 0 ok; 1 the name was refused (empty, whitespace-only, too long);
2 the command refused to run (schema not at head, database error, bad usage).
"""

from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import app.db as app_db
from app.models.user import Team
from app.schema_check import SchemaNotAtHeadError, assert_schema_at_head
from app.services.audit import write_audit_log

EXIT_OK = 0
EXIT_BAD_NAME = 1
EXIT_REFUSED = 2

# `Team.name` is an unbounded VARCHAR, so this is the command's own limit.
TEAM_NAME_MAX_CHARS = 100

OPERATOR_ACTOR = "operator command"

_USAGE = 'usage: python -m app.teams create "<name>" | list'


def _name_problem(name: str) -> str | None:
    if not name:
        return "the team name is empty"
    if len(name) > TEAM_NAME_MAX_CHARS:
        return f"the team name is {len(name)} characters; the limit is {TEAM_NAME_MAX_CHARS}"
    return None


def create_team(db, name: str) -> tuple[Team, bool]:
    """(team, created). Idempotent by exact name."""
    existing = db.execute(select(Team).where(Team.name == name)).scalar_one_or_none()
    if existing is not None:
        return existing, False
    team = Team(name=name)
    db.add(team)
    try:
        db.flush()
    except IntegrityError:
        # Another process inserted the same name between the select and the insert.
        db.rollback()
        return db.execute(select(Team).where(Team.name == name)).scalar_one(), False
    write_audit_log(
        db,
        actor_id=None,
        action="team.created",
        target=f"team:{team.id}",
        metadata={"actor": OPERATOR_ACTOR, "name": name},
    )
    db.commit()
    return team, True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("create", "list") or len(args) != (2 if args[0] == "create" else 1):
        print(f"teams: {_USAGE}", file=sys.stderr)
        return EXIT_REFUSED
    command = args[0]

    name = ""
    if command == "create":
        name = args[1].strip()
        problem = _name_problem(name)
        if problem:
            print(f"teams: refused -- {problem}", file=sys.stderr)
            return EXIT_BAD_NAME

    try:
        # Read at call time through the module, like the server and the worker.
        assert_schema_at_head(app_db.engine)
        db = app_db.SessionLocal()
        try:
            if command == "list":
                for team in db.execute(select(Team).order_by(Team.created_at, Team.name)).scalars():
                    print(f"{team.id}\t{team.name}")
            else:
                team, created = create_team(db, name)
                print(f"{team.id}\t{team.name}")
                print(
                    "teams: created" if created else "teams: already exists, nothing changed",
                    file=sys.stderr,
                )
        finally:
            db.close()
    except SchemaNotAtHeadError as exc:
        print(f"teams: refused -- {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except Exception as exc:  # noqa: BLE001 -- the class only; the message may carry the DSN
        print(f"teams: database error ({type(exc).__name__})", file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
