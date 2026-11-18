"""prod seeding must insert the template only, never dev users."""

from app.models.template import Template
from app.models.user import Team, User
from app.seed import GENERIC_WEBTOOL_TEMPLATE_NAME, SEED_USERS, seed


def test_dev_seed_inserts_template_users_and_team(db_session):
    # db_session fixture already ran seed() once (dev mode); assert the
    # dev fixture state itself carries the users/team.
    assert db_session.query(Template).filter_by(name=GENERIC_WEBTOOL_TEMPLATE_NAME).first()
    assert db_session.query(Team).count() >= 1
    assert db_session.query(User).count() >= len(SEED_USERS)


def test_prod_seed_excludes_dev_users_and_team(db_session):
    # Wipe what the fixture already seeded, then re-seed in prod mode to
    # verify the prod branch inserts the template only.
    db_session.query(User).delete()
    db_session.query(Team).delete()
    db_session.query(Template).delete()
    db_session.commit()

    seed(db_session, is_prod=True)

    assert db_session.query(Template).filter_by(name=GENERIC_WEBTOOL_TEMPLATE_NAME).first() is not None
    assert db_session.query(Team).count() == 0
    assert db_session.query(User).count() == 0


def test_prod_seed_is_idempotent(db_session):
    db_session.query(User).delete()
    db_session.query(Team).delete()
    db_session.query(Template).delete()
    db_session.commit()

    seed(db_session, is_prod=True)
    seed(db_session, is_prod=True)

    assert db_session.query(Template).count() == 1
