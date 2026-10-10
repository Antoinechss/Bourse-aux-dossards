import pytest

from app.models import Listing, RaceRulesRow, Registration, WaitlistEntry
from app.transfers.state import TransferState
from scripts.demo_offer import create_offer


@pytest.fixture
def registrations(session, race):
    session.add_all(
        [
            Registration(race_id=race.id, bib_number=102, first_name="Théo", last_name="Marchand"),
            Registration(race_id=race.id, bib_number=101, first_name="Camille", last_name="Roussel"),
        ]
    )
    session.commit()


def test_create_offer_reserves_the_first_bib_for_a_buyer(session, race, registrations):
    transfer = create_offer(session, "vendeur@example.com")

    listing = session.get(Listing, transfer.listing_id)
    assert transfer.state is TransferState.RESERVE
    assert (listing.bib_number, listing.seller_email, listing.price_cents) == (101, "vendeur@example.com", 4500)
    assert session.get(WaitlistEntry, transfer.buyer_id).first_name == "Marie"
    assert transfer.offer_expires_at is not None


def test_create_offer_twice_uses_another_bib(session, race, registrations):
    create_offer(session, "vendeur@example.com")
    second = create_offer(session, "vendeur@example.com")

    assert session.get(Listing, second.listing_id).bib_number == 102


def test_create_offer_respects_the_resale_cap(session, race, registrations):
    session.add(RaceRulesRow(race_id=race.id, max_resale_price_cents=2200))
    session.commit()

    transfer = create_offer(session, "vendeur@example.com")

    assert session.get(Listing, transfer.listing_id).price_cents == 2200


def test_create_offer_needs_a_race_and_a_free_bib(session, race):
    with pytest.raises(SystemExit):
        create_offer(session, "vendeur@example.com")  # course sans inscrit
