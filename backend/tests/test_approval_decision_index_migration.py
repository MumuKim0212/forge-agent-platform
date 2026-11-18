"""migration `d2c8f5a71e39` adds the
partial unique index that lets the database refuse a second approve decision by one actor on a
request, after removing -- and auditing -- the duplicates that may already exist.

The scenario is `tests/approval_decision_migration_support.py`'s, shared with the Postgres
run in `test_approval_postgres.py`. `tmp_path` SQLite files only (alembic needs a file that
survives connections); nothing here reads the ambient `.env` or touches the dev DB. The
chain-wide properties (model == migrations, every downgrade restores the previous schema) are
`tests/test_migrations_parity.py`'s.
"""

from __future__ import annotations

from tests import approval_decision_migration_support as scenario
from tests import migration_parity_support as parity


def test_duplicates_are_removed_audited_and_the_index_enforces_one_approve_per_actor(tmp_path):
    scenario.run_data_scenario(parity.sqlite_url(tmp_path / "dedupe.db"))


def test_a_database_without_duplicates_is_not_touched(tmp_path):
    scenario.run_clean_database_scenario(parity.sqlite_url(tmp_path / "clean.db"))
