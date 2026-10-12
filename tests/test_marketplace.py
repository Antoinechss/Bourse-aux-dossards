import dataclasses
from datetime import date, timedelta

import pytest
from sqlmodel import select

from app.config import settings
from app.marketplace import service as market
from app.marketplace.service import Ineligible, MarketError, RaceMarket
from app.models import Buyer, Listing, Notification, RaceRulesRow, Registration, Transfer, utcnow
from app.transfers.state import TransferState

TODAY = date(2026, 10, 10)
MARIE = {"first_name": "Marie", "last_name": "Martin", "email": "marie@example.com", "birth_date": date(1990, 5, 2)}


@pytest.fixture
def rules(session, race):
    """Revente autorisée jusqu'au 28 mars 2027, plafonnée à 45 € ; la course demande 18 ans."""
    race.minimum_age = 18
    rules = RaceRulesRow(
        race_id=race.id, transfer_allowed=True, transfer_deadline=date(2027, 3, 28), max_resale_price_cents=4500
    )
    session.add_all(
        [
            race,
            rules,
            Registration(race_id=race.id, bib_number=42, first_name="Jean", last_name="Dupont"),
            Registration(race_id=race.id, bib_number=43, first_name="Paul", last_name="Durand"),
        ]
    )
    session.commit()
    return rules


def sell(session, race, **changes) -> Transfer:
    """Jean Dupont met son dossard 42 en vente à 45 €, sauf ce qui est passé en argument."""
    values = {
        "bib_number": 42,
        "first_name": "Jean",
        "last_name": "Dupont",
        "seller_email": "jean@example.com",
        "price_cents": 4500,
        "today": TODAY,
    }
    return market.create_listing(session, race, **{**values, **changes})


def messages(session, email: str) -> list[Notification]:
    return list(session.exec(select(Notification).where(Notification.recipient_email == email).order_by(Notification.id)))


def transfers(session) -> list[Transfer]:
    return list(session.exec(select(Transfer).order_by(Transfer.id)))


# --- Mise en vente : seul un dossard vérifié est en vente ---


def test_a_verified_bib_goes_on_sale(session, race, rules):
    transfer = sell(session, race)

    assert transfer.state is TransferState.EN_VENTE
    assert transfer.buyer_id is None
    listing = session.get(Listing, transfer.listing_id)
    assert (listing.bib_number, listing.price_cents, listing.seller_email) == (42, 4500, "jean@example.com")
    [message] = messages(session, "jean@example.com")
    assert "Votre dossard n°42 est vérifié et en vente" in message.subject
    assert message.link == f"/runner/listings/{listing.id}"


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"bib_number": 99}, "Aucun dossard n°99"),  # inconnu de l'organisateur
        ({"first_name": "Paul", "last_name": "Durand"}, "n'est pas inscrit à ce nom"),  # le dossard d'un autre
    ],
)
def test_a_bib_the_organizer_cannot_verify_is_never_on_sale(session, race, rules, changes, expected):
    with pytest.raises(MarketError, match=expected):
        sell(session, race, **changes)

    assert session.exec(select(Listing)).first() is None
    assert transfers(session) == []


def test_verification_ignores_case_and_accents(session, race, rules):
    assert sell(session, race, first_name="jean", last_name="DUPONT").state is TransferState.EN_VENTE


def test_price_above_the_cap_is_refused_and_nothing_is_saved(session, race, rules):
    with pytest.raises(MarketError, match="plafonne la revente à 45,00 €"):
        sell(session, race, price_cents=4501)

    assert session.exec(select(Listing)).first() is None


def test_any_price_is_accepted_when_the_rules_set_no_cap(session, race, rules):
    rules.max_resale_price_cents = None
    session.add(rules)
    session.commit()

    assert sell(session, race, price_cents=9900).state is TransferState.EN_VENTE


def test_price_must_be_positive(session, race, rules):
    with pytest.raises(MarketError, match="supérieur à zéro"):
        sell(session, race, price_cents=0)


def test_listing_is_refused_when_the_rules_forbid_resale(session, race, rules):
    rules.transfer_allowed = False
    session.add(rules)
    session.commit()

    with pytest.raises(MarketError, match="interdit la revente"):
        sell(session, race)


def test_listing_is_accepted_on_the_deadline_and_refused_the_day_after(session, race, rules):
    with pytest.raises(MarketError, match="clos depuis le 28/03/2027"):
        sell(session, race, today=date(2027, 3, 29))

    assert sell(session, race, today=date(2027, 3, 28)).state is TransferState.EN_VENTE


def test_listing_is_refused_after_the_race(session, race, rules):
    with pytest.raises(MarketError, match="déjà eu lieu"):
        sell(session, race, today=date(2027, 4, 12))


def test_a_bib_cannot_be_listed_twice(session, race, rules):
    sell(session, race)

    with pytest.raises(MarketError, match="déjà en vente"):
        sell(session, race)


# --- Réservation : « Acheter ce dossard » ---


def test_buying_reserves_the_bib_for_fifteen_minutes(session, race, rules):
    transfer = sell(session, race)
    now = utcnow()

    reserved = market.reserve(session, transfer.id, now=now)

    assert reserved.state is TransferState.RESERVE
    assert reserved.offer_expires_at == now + timedelta(minutes=15)


def test_the_reservation_time_is_a_setting(session, race, rules, monkeypatch):
    monkeypatch.setattr(market, "settings", dataclasses.replace(settings, reservation_minutes=5))
    transfer = sell(session, race)
    now = utcnow()

    assert market.reserve(session, transfer.id, now=now).offer_expires_at == now + timedelta(minutes=5)


def test_a_reserved_bib_cannot_be_reserved_by_someone_else(session, race, rules):
    transfer = sell(session, race)
    market.reserve(session, transfer.id)

    with pytest.raises(MarketError, match="vient d'être réservé"):
        market.reserve(session, transfer.id)


# --- Contrôles de l'acheteur, avant tout paiement ---


def reserved(session, race) -> Transfer:
    return market.reserve(session, sell(session, race).id)


def test_an_eligible_buyer_can_go_on_to_pay(session, race, rules):
    transfer = reserved(session, race)

    buyer = market.register_buyer(session, transfer, **MARIE)

    assert transfer.state is TransferState.ELIGIBLE
    assert transfer.buyer_id == buyer.id
    assert (buyer.first_name, buyer.last_name, buyer.birth_date) == ("Marie", "Martin", date(1990, 5, 2))


def test_a_buyer_too_young_is_stopped_and_the_bib_goes_back_on_sale(session, race, rules):
    transfer = reserved(session, race)

    with pytest.raises(Ineligible, match="Il faut avoir 18 ans le jour de la course"):
        market.register_buyer(session, transfer, **{**MARIE, "birth_date": date(2010, 1, 1)})

    stopped, fresh = transfers(session)
    assert stopped.state is TransferState.INELIGIBLE
    assert (fresh.listing_id, fresh.state, fresh.buyer_id) == (stopped.listing_id, TransferState.EN_VENTE, None)


def test_age_is_counted_on_race_day(session, race, rules):
    # La course a lieu le 11 avril 2027 : avoir 18 ans ce jour-là suffit, les avoir le lendemain ne suffit pas.
    assert market.eligibility_problem(session, race, "Marie", "Martin", date(2009, 4, 11)) is None
    assert market.eligibility_problem(session, race, "Marie", "Martin", date(2009, 4, 12)) is not None


@pytest.mark.parametrize("first_name, last_name", [("Paul", "Durand"), ("jean", "DUPONT")])
def test_a_runner_already_registered_cannot_buy_a_second_bib(session, race, rules, first_name, last_name):
    transfer = reserved(session, race)

    with pytest.raises(Ineligible, match="déjà inscrit à cette course"):
        market.register_buyer(session, transfer, **{**MARIE, "first_name": first_name, "last_name": last_name})

    assert transfer.state is TransferState.INELIGIBLE


def test_identity_is_refused_once_the_reservation_has_run_out(session, race, rules):
    transfer = reserved(session, race)

    with pytest.raises(MarketError, match="n'est plus valable"):
        market.register_buyer(session, transfer, **MARIE, now=utcnow() + timedelta(minutes=16))

    assert session.exec(select(Buyer)).first() is None


def test_identity_is_refused_for_a_bib_that_was_not_reserved(session, race, rules):
    transfer = sell(session, race)  # en vente, personne n'a cliqué « Acheter »

    with pytest.raises(MarketError, match="n'est plus valable"):
        market.register_buyer(session, transfer, **MARIE)

    assert transfer.state is TransferState.EN_VENTE


# --- Expiration d'une réservation ---


def test_a_reservation_not_paid_in_time_puts_the_bib_back_on_sale(session, race, rules):
    transfer = reserved(session, race)
    market.register_buyer(session, transfer, **MARIE)

    assert market.expire_reservations(session, now=utcnow() + timedelta(minutes=14)) == 0
    assert market.expire_reservations(session, now=utcnow() + timedelta(minutes=16)) == 1

    expired, fresh = transfers(session)
    assert expired.state is TransferState.RESERVATION_EXPIREE
    assert (fresh.listing_id, fresh.state, fresh.buyer_id) == (expired.listing_id, TransferState.EN_VENTE, None)
    assert "Votre réservation a expiré" in messages(session, "marie@example.com")[-1].subject


def test_a_reservation_expires_even_before_the_buyer_gave_their_identity(session, race, rules):
    reserved(session, race)

    assert market.expire_reservations(session, now=utcnow() + timedelta(minutes=16)) == 1
    assert [t.state for t in transfers(session)] == [TransferState.RESERVATION_EXPIREE, TransferState.EN_VENTE]


def test_a_paid_bib_never_expires(session, race, rules):
    transfer = reserved(session, race)
    market.register_buyer(session, transfer, **MARIE)
    transfer.state = TransferState.AUTORISE
    session.add(transfer)
    session.commit()

    assert market.expire_reservations(session, now=utcnow() + timedelta(days=2)) == 0
    assert transfer.state is TransferState.AUTORISE


# --- Liste d'attente : être prévenu d'une mise en vente ---


def test_runners_on_the_waitlist_are_told_when_a_bib_goes_on_sale(session, race, rules):
    market.join_waitlist(session, race, "Karim", "Benali", "karim@example.com", today=TODAY)

    sell(session, race)

    joined, alert = messages(session, "karim@example.com")
    assert "Vous êtes sur la liste d'attente" in joined.subject
    assert "Un dossard est en vente" in alert.subject
    assert alert.link == f"/runner/races/{race.id}"


def test_joining_twice_with_the_same_email_is_refused(session, race, rules):
    market.join_waitlist(session, race, "Karim", "Benali", "karim@example.com", today=TODAY)

    with pytest.raises(MarketError, match="déjà inscrite"):
        market.join_waitlist(session, race, "Karim", "Benali", "karim@example.com", today=TODAY)


def test_joining_is_refused_when_the_rules_forbid_resale(session, race, rules):
    rules.transfer_allowed = False
    session.add(rules)
    session.commit()

    with pytest.raises(MarketError, match="interdit la revente"):
        market.join_waitlist(session, race, "Karim", "Benali", "karim@example.com", today=TODAY)


def test_market_counts_and_shows_free_bibs_first(session, race, rules):
    market.join_waitlist(session, race, "Karim", "Benali", "karim@example.com", today=TODAY)
    market.reserve(session, sell(session, race).id)  # le 42 est réservé
    sell(session, race, bib_number=43, first_name="Paul", last_name="Durand", seller_email="paul@example.com")

    assert market.race_market(session, race.id) == RaceMarket(free=1, reserved=1, waiting=1)
    assert [listing.bib_number for listing, _ in market.bibs_on_sale(session, race.id)] == [43, 42]
