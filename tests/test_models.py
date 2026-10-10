from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    LedgerEntry,
    LedgerKind,
    Listing,
    ProofType,
    RaceRulesRow,
    Registration,
    Transfer,
    Verdict,
    Verification,
    WaitlistEntry,
)
from app.transfers.state import TransferState


def test_race_default_commission_is_8_percent(race):
    assert race.platform_fee_bps == 500
    assert race.organizer_fee_bps == 300


def test_rules_are_none_when_the_reglement_is_silent(session, race):
    rules = RaceRulesRow(
        race_id=race.id,
        max_resale_price_cents=4500,
        max_resale_price_quote="Le dossard ne peut être revendu plus de 45 €.",
        transfer_deadline=date(2027, 3, 28),
        transfer_deadline_quote="Aucun transfert après le 28 mars 2027.",
    )
    session.add(rules)
    session.commit()
    session.expire_all()  # force la relecture depuis la base

    reloaded = session.get(RaceRulesRow, rules.id)
    assert reloaded.max_resale_price_cents == 4500
    assert reloaded.transfer_deadline == date(2027, 3, 28)
    assert reloaded.transfer_allowed is None
    assert reloaded.transfer_allowed_quote is None


def test_a_race_has_only_one_set_of_rules(session, race):
    session.add(RaceRulesRow(race_id=race.id))
    session.commit()

    session.add(RaceRulesRow(race_id=race.id))
    with pytest.raises(IntegrityError):
        session.commit()


def test_new_transfer_starts_en_vente_without_buyer(session, race):
    listing = Listing(race_id=race.id, bib_number=42, seller_email="vendeur@example.com", price_cents=4500)
    session.add(listing)
    session.commit()

    transfer = Transfer(listing_id=listing.id)
    session.add(transfer)
    session.commit()
    session.refresh(transfer)

    assert transfer.state is TransferState.EN_VENTE
    assert transfer.buyer_id is None
    assert transfer.created_at is not None


def test_full_transfer_round_trips_through_the_database(session, race):
    listing = Listing(race_id=race.id, bib_number=42, seller_email="vendeur@example.com", price_cents=4500)
    buyer = WaitlistEntry(race_id=race.id, first_name="Marie", last_name="Martin", email="marie@example.com")
    session.add_all([listing, buyer])
    session.commit()

    transfer = Transfer(
        listing_id=listing.id,
        buyer_id=buyer.id,
        state=TransferState.AUTORISE,
        paypal_authorization_id="AUTH-123",
    )
    session.add(transfer)
    session.commit()
    session.add_all(
        [
            Verification(
                transfer_id=transfer.id,
                proof_type=ProofType.LICENCE_FFA,
                verdict=Verdict.ORANGE,
                explanation="Nom différent de celui de la licence",
            ),
            LedgerEntry(transfer_id=transfer.id, kind=LedgerKind.AUTORISATION, amount_cents=4860),
        ]
    )
    session.commit()
    session.expire_all()  # force la relecture depuis la base

    reloaded = session.get(Transfer, transfer.id)
    verification = session.get(Verification, 1)
    entry = session.get(LedgerEntry, 1)
    assert reloaded.state is TransferState.AUTORISE
    assert reloaded.created_at.tzinfo is not None
    assert verification.proof_type is ProofType.LICENCE_FFA
    assert verification.verdict is Verdict.ORANGE
    assert verification.organizer_approved is None
    assert entry.kind is LedgerKind.AUTORISATION
    assert entry.amount_cents == 4860
    assert entry.currency == "EUR"


def test_bib_number_is_unique_within_a_race(session, race):
    session.add(Registration(race_id=race.id, bib_number=42, first_name="Jean", last_name="Dupont"))
    session.commit()

    session.add(Registration(race_id=race.id, bib_number=42, first_name="Paul", last_name="Durand"))
    with pytest.raises(IntegrityError):
        session.commit()
