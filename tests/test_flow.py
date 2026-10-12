"""Tests de la suite d'un paiement autorisé (app/transfers/flow.py) : PayPal est remplacé par un faux."""

from datetime import timedelta

import pytest
from paypal_fakes import FakePayPal, reserved_transfer
from sqlmodel import select

from app.models import LedgerKind, Notification, Registration, Transfer, utcnow
from app.payments import ledger, payouts, service
from app.payments.client import PayPalError
from app.registry_mock.registry import get_registration
from app.transfers import flow
from app.transfers.state import TransferState


@pytest.fixture
def paypal(monkeypatch):
    fake = FakePayPal()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def transfer(session, race, paypal):
    """Marie Martin a payé le dossard 42 de Jean Dupont : le paiement est autorisé, rien n'est encore débité."""
    session.add(Registration(race_id=race.id, bib_number=42, first_name="Jean", last_name="Dupont"))
    transfer = reserved_transfer(session, race)
    service.create_order(session, transfer)
    service.authorize(session, transfer)
    set_state(session, transfer, TransferState.AUTORISE)
    return transfer


def set_state(session, transfer, state: TransferState) -> None:
    transfer.state = state
    session.add(transfer)
    session.commit()


def subjects(session, email: str) -> list[str]:
    statement = select(Notification.subject).where(Notification.recipient_email == email).order_by(Notification.id)
    return list(session.exec(statement))


def test_advance_changes_the_name_captures_and_sends_the_payouts(session, race, transfer, paypal):
    flow.advance(session, transfer)

    holder = get_registration(session, race.id, 42)
    assert (holder.first_name, holder.last_name) == ("Marie", "Martin")
    # ENCAISSE, pas encore VERSE : PayPal n'a pas confirmé le lot.
    assert transfer.state is TransferState.ENCAISSE
    assert paypal.operations()[-2:] == ["capture_authorization", "send_payouts"]
    kinds = [entry.kind for entry in ledger.entries(session, transfer.id)]
    assert kinds == [
        LedgerKind.AUTORISATION,
        LedgerKind.CAPTURE,
        LedgerKind.VERSEMENT_VENDEUR,
        LedgerKind.VERSEMENT_ORGANISATEUR,
    ]
    assert subjects(session, "marie@example.com") == ["Le dossard n°42 est à votre nom — Trail des Crêtes"]


def test_advance_resumes_where_paypal_failed(session, race, transfer, paypal):
    paypal.error = PayPalError("panne simulée")

    with pytest.raises(PayPalError):
        flow.advance(session, transfer)

    # Le nom est changé, l'argent n'a pas bougé.
    assert transfer.state is TransferState.NOM_CHANGE
    assert ledger.find(session, transfer.id, LedgerKind.CAPTURE) is None

    paypal.error = None
    flow.advance(session, transfer)

    assert transfer.state is TransferState.ENCAISSE
    assert paypal.operations().count("send_payouts") == 1


def test_advance_twice_does_not_pay_twice(session, transfer, paypal):
    flow.advance(session, transfer)
    flow.advance(session, transfer)

    assert paypal.operations().count("capture_authorization") == 1
    assert paypal.operations().count("send_payouts") == 1


def test_advance_leaves_an_unpaid_transfer_alone(session, race, transfer, paypal):
    set_state(session, transfer, TransferState.ELIGIBLE)
    calls = len(paypal.calls)

    flow.advance(session, transfer)

    assert transfer.state is TransferState.ELIGIBLE
    assert len(paypal.calls) == calls
    assert get_registration(session, race.id, 42).first_name == "Jean"  # le dossard n'a pas changé de nom


def test_check_payout_marks_the_transfer_paid_once_paypal_confirms(session, transfer, paypal):
    flow.advance(session, transfer)

    assert flow.check_payout(session, transfer) is True

    assert transfer.state is TransferState.VERSE
    assert subjects(session, "vendeur@example.com") == ["Vous avez été payé — dossard n°42"]
    assert flow.check_payout(session, transfer) is False  # plus rien à confirmer


@pytest.mark.parametrize(
    "batch",
    [
        payouts.PayoutBatch(id="BATCH-1", status="PROCESSING"),
        payouts.PayoutBatch(id="BATCH-1", status="SUCCESS", item_statuses={"a": "SUCCESS", "b": "UNCLAIMED"}),
    ],
)
def test_check_payout_waits_until_every_payout_has_arrived(session, transfer, paypal, monkeypatch, batch):
    flow.advance(session, transfer)
    monkeypatch.setattr(payouts, "get_payout_batch", lambda batch_id: batch)

    assert flow.check_payout(session, transfer) is False
    assert transfer.state is TransferState.ENCAISSE


def test_housekeeping_takes_a_paid_transfer_to_the_end(session, transfer, paypal):
    flow.housekeeping(session)

    assert session.get(Transfer, transfer.id).state is TransferState.VERSE


def test_housekeeping_expires_reservations(session, race, paypal):
    reservation = reserved_transfer(session, race)
    reservation.offer_expires_at = utcnow() - timedelta(minutes=1)
    session.add(reservation)
    session.commit()

    flow.housekeeping(session)

    assert session.get(Transfer, reservation.id).state is TransferState.RESERVATION_EXPIREE


def test_housekeeping_survives_a_failing_transfer(session, transfer, paypal):
    paypal.error = PayPalError("panne simulée")

    flow.housekeeping(session)  # ne lève pas

    assert session.get(Transfer, transfer.id).state is TransferState.NOM_CHANGE

    paypal.error = None
    flow.housekeeping(session)

    assert session.get(Transfer, transfer.id).state is TransferState.VERSE
