from sqlmodel import select

from app.models import Race, RaceRulesRow, Registration
from scripts.seed import seed

ORGANIZER = "orga@example.com"

# Chaque règle et la colonne qui contient sa citation.
RULES_AND_QUOTES = [
    ("transfer_allowed", "transfer_allowed_quote"),
    ("max_resale_price_cents", "max_resale_price_quote"),
    ("transfer_deadline", "transfer_deadline_quote"),
    ("transfer_fee_cents", "transfer_fee_quote"),
    ("required_proofs", "required_proofs_quote"),
]


def test_seed_creates_demo_races_with_rules_and_registrations(session):
    count = seed(session, ORGANIZER)

    races = session.exec(select(Race)).all()
    assert count == len(races) == 3
    for race in races:
        assert race.organizer_email == ORGANIZER
        rules = session.exec(select(RaceRulesRow).where(RaceRulesRow.race_id == race.id)).one()
        assert rules.transfer_allowed is True
        registrations = session.exec(select(Registration).where(Registration.race_id == race.id)).all()
        assert len(registrations) == 8


def test_every_rule_has_a_quote_and_every_quote_has_a_rule(session):
    seed(session, ORGANIZER)

    for rules in session.exec(select(RaceRulesRow)).all():
        for rule, quote in RULES_AND_QUOTES:
            assert (getattr(rules, rule) is None) == (getattr(rules, quote) is None), (rules.race_id, rule)


def test_transfer_deadlines_fall_before_the_race(session):
    seed(session, ORGANIZER)

    for rules in session.exec(select(RaceRulesRow)).all():
        race = session.get(Race, rules.race_id)
        if rules.transfer_deadline is not None:
            assert rules.transfer_deadline < race.event_date


def test_seed_twice_does_not_duplicate(session):
    seed(session, ORGANIZER)

    assert seed(session, ORGANIZER) == 0
    assert len(session.exec(select(Race)).all()) == 3
    assert len(session.exec(select(Registration)).all()) == 24
