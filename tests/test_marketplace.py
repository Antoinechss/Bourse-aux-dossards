from datetime import date, timedelta

import pytest
from sqlmodel import select

from app.marketplace import service as market
from app.marketplace.service import MarketError, RaceMarket
from app.models import Listing, Notification, RaceRulesRow, Registration, Transfer, WaitlistEntry, utcnow
from app.transfers.state import TransferState

TODAY = date(2026, 10, 10)


@pytest.fixture
def rules(session, race):
    """Revente autorisée jusqu'au 28 mars 2027, plafonnée à 45 €."""
    rules = RaceRulesRow(
        race_id=race.id, transfer_allowed=True, transfer_deadline=date(2027, 3, 28), max_resale_price_cents=4500
    )
    session.add(rules)
    session.add_all(
        [
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


def join(session, race, first_name: str, email: str | None = None) -> WaitlistEntry:
    return market.join_waitlist(session, race, first_name, "Martin", email or f"{first_name.lower()}@example.com", today=TODAY)


def messages(session, email: str) -> list[Notification]:
    return list(session.exec(select(Notification).where(Notification.recipient_email == email).order_by(Notification.id)))


# --- Mise en vente ---


def test_listing_without_waitlist_stays_on_sale(session, race, rules):
    transfer = sell(session, race)

    assert transfer.state is TransferState.EN_VENTE
    assert transfer.buyer_id is None
    listing = session.get(Listing, transfer.listing_id)
    assert (listing.bib_number, listing.price_cents, listing.seller_email) == (42, 4500, "jean@example.com")
    [message] = messages(session, "jean@example.com")
    assert "Votre dossard n°42 est en vente" in message.subject
    assert message.link == f"/runner/listings/{listing.id}"


def test_price_above_the_cap_is_refused_and_nothing_is_saved(session, race, rules):
    with pytest.raises(MarketError, match="plafonne la revente à 45,00 €"):
        sell(session, race, price_cents=4501)

    assert session.exec(select(Listing)).first() is None
    assert session.exec(select(Transfer)).first() is None


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


def test_listing_is_refused_after_the_race(session, race):
    with pytest.raises(MarketError, match="déjà eu lieu"):
        sell(session, race, today=date(2027, 4, 12))


def test_only_the_registered_holder_can_sell_the_bib(session, race, rules):
    with pytest.raises(MarketError, match="Aucun dossard n°99"):
        sell(session, race, bib_number=99)
    with pytest.raises(MarketError, match="n'est pas inscrit à ce nom"):
        sell(session, race, first_name="Paul", last_name="Durand")

    # La casse et les accents ne comptent pas.
    assert sell(session, race, first_name="jean", last_name="DUPONT").state is TransferState.EN_VENTE


def test_a_bib_cannot_be_listed_twice(session, race, rules):
    sell(session, race)

    with pytest.raises(MarketError, match="déjà en vente"):
        sell(session, race)


# --- Liste d'attente et offres ---


def test_listing_is_offered_to_the_first_runner_of_the_waitlist(session, race, rules):
    first, second = join(session, race, "Marie"), join(session, race, "Karim")

    transfer = sell(session, race)

    assert transfer.state is TransferState.RESERVE
    assert transfer.buyer_id == first.id
    assert timedelta(hours=47) < transfer.offer_expires_at - utcnow() <= timedelta(hours=48)
    offer = messages(session, "marie@example.com")[-1]
    assert "Un dossard est disponible pour vous" in offer.subject
    assert "45,00 €" in offer.body
    assert offer.link == f"/runner/transfers/{transfer.id}"
    assert market.position(session, first) is None  # elle a une offre
    assert market.position(session, second) == 1


def test_joining_the_waitlist_gets_a_bib_that_was_waiting(session, race, rules):
    transfer = sell(session, race)

    entry = join(session, race, "Marie")

    session.refresh(transfer)
    assert transfer.state is TransferState.RESERVE
    assert market.current_offer(session, entry).id == transfer.id


def test_waitlist_positions_follow_the_order_of_arrival(session, race, rules):
    first, second = join(session, race, "Marie"), join(session, race, "Karim")

    assert (market.position(session, first), market.position(session, second)) == (1, 2)
    assert market.current_offer(session, first) is None
    assert "liste d'attente" in messages(session, "marie@example.com")[0].subject


def test_joining_twice_with_the_same_email_is_refused(session, race, rules):
    join(session, race, "Marie")

    with pytest.raises(MarketError, match="déjà inscrite"):
        join(session, race, "Marie")


def test_joining_is_refused_when_the_rules_forbid_resale(session, race, rules):
    rules.transfer_allowed = False
    session.add(rules)
    session.commit()

    with pytest.raises(MarketError, match="interdit la revente"):
        join(session, race, "Marie")


def test_the_seller_is_never_offered_their_own_bib(session, race, rules):
    join(session, race, "Jean", email="jean@example.com")
    other = join(session, race, "Marie")

    assert sell(session, race).buyer_id == other.id


def test_a_runner_with_an_offer_in_progress_is_skipped(session, race, rules):
    _, second = join(session, race, "Marie"), join(session, race, "Karim")
    sell(session, race)  # proposé à Marie

    other = sell(session, race, bib_number=43, first_name="Paul", last_name="Durand", seller_email="paul@example.com")

    assert other.buyer_id == second.id


def test_market_counts(session, race, rules):
    join(session, race, "Marie")
    sell(session, race)  # proposé à Marie
    sell(session, race, bib_number=43, first_name="Paul", last_name="Durand", seller_email="paul@example.com")  # personne
    join(session, race, "Jean", email="jean@example.com")  # le vendeur du 42 : on lui propose le 43

    assert market.race_market(session, race.id) == RaceMarket(free=0, offered=2, waiting=0)


# --- Expiration ---


def test_expired_offer_goes_to_the_next_runner(session, race, rules):
    _, second = join(session, race, "Marie"), join(session, race, "Karim")
    transfer = sell(session, race)

    assert market.expire_offers(session, now=utcnow() + timedelta(hours=47)) == 0
    assert market.expire_offers(session, now=utcnow() + timedelta(hours=49)) == 1

    session.refresh(transfer)
    assert transfer.state is TransferState.OFFRE_EXPIREE
    fresh = session.exec(select(Transfer).where(Transfer.id != transfer.id)).one()
    assert (fresh.listing_id, fresh.state, fresh.buyer_id) == (transfer.listing_id, TransferState.RESERVE, second.id)
    assert "Votre offre a expiré" in messages(session, "marie@example.com")[-1].subject
    assert "Un dossard est disponible" in messages(session, "karim@example.com")[-1].subject


def test_a_runner_is_not_offered_again_a_bib_they_let_expire(session, race, rules):
    first = join(session, race, "Marie")
    transfer = sell(session, race)

    market.expire_offers(session, now=utcnow() + timedelta(hours=49))

    fresh = session.exec(select(Transfer).where(Transfer.id != transfer.id)).one()
    assert (fresh.state, fresh.buyer_id) == (TransferState.EN_VENTE, None)
    # Elle reste sur la liste d'attente, pour un autre dossard.
    assert market.position(session, first) == 1
