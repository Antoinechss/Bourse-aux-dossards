"""Tests de app/payments/service.py : PayPal est remplacé par un faux, la base est en mémoire."""

import pytest
from paypal_fakes import FakePayPal, reserved_transfer

from app.models import LedgerKind, Listing, Transfer
from app.payments import ledger, service
from app.payments.client import PayPalError
from app.transfers.state import TransferState


@pytest.fixture
def paypal(monkeypatch):
    fake = FakePayPal()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def transfer(session, race):
    return reserved_transfer(session, race)


def ledger_lines(session, transfer):
    return [(e.kind, e.amount_cents, e.paypal_id) for e in ledger.entries(session, transfer.id)]


def test_create_order_asks_the_buyer_for_price_plus_service_fee(session, transfer, paypal):
    order = service.create_order(session, transfer)

    assert order.id == "ORDER-1"
    assert paypal.order_request["amounts"].total_cents == 4860
    assert paypal.order_request["description"] == "Dossard n°42 — Trail des Crêtes"
    assert session.get(Transfer, transfer.id).paypal_order_id == "ORDER-1"
    # Rien n'est encore bloqué : le journal reste vide.
    assert ledger_lines(session, transfer) == []


def test_authorize_blocks_the_funds_and_writes_the_ledger(session, transfer, paypal):
    service.create_order(session, transfer)

    service.authorize(session, transfer)

    reloaded = session.get(Transfer, transfer.id)
    assert reloaded.paypal_authorization_id == "AUTH-1"
    assert reloaded.authorized_at is not None
    assert ledger_lines(session, transfer) == [(LedgerKind.AUTORISATION, 4860, "AUTH-1")]


def test_authorize_without_an_order_is_refused(session, transfer, paypal):
    with pytest.raises(service.PaymentError):
        service.authorize(session, transfer)

    assert paypal.calls == []


def test_denied_authorization_records_nothing(session, transfer, paypal):
    service.create_order(session, transfer)
    paypal.authorization_status = "DENIED"

    with pytest.raises(PayPalError):
        service.authorize(session, transfer)

    assert session.get(Transfer, transfer.id).paypal_authorization_id is None
    assert ledger_lines(session, transfer) == []


def test_create_order_is_refused_once_the_funds_are_blocked(session, transfer, paypal):
    service.create_order(session, transfer)
    service.authorize(session, transfer)

    with pytest.raises(service.PaymentError):
        service.create_order(session, transfer)


def test_capture_debits_the_buyer_and_writes_the_ledger(session, transfer, paypal):
    service.create_order(session, transfer)
    service.authorize(session, transfer)

    service.capture(session, transfer)

    assert ledger_lines(session, transfer)[-1] == (LedgerKind.CAPTURE, 4860, "CAPT-1")


def test_capture_without_authorization_is_refused(session, transfer, paypal):
    with pytest.raises(service.PaymentError):
        service.capture(session, transfer)

    assert paypal.calls == []


def test_pending_capture_is_not_counted_as_money_received(session, transfer, paypal):
    service.create_order(session, transfer)
    service.authorize(session, transfer)
    paypal.capture_status = "PENDING"

    with pytest.raises(PayPalError):
        service.capture(session, transfer)

    assert ledger.find(session, transfer.id, LedgerKind.CAPTURE) is None


def test_void_releases_the_funds_and_writes_the_ledger(session, transfer, paypal):
    service.create_order(session, transfer)
    service.authorize(session, transfer)

    service.void(session, transfer)

    assert ledger_lines(session, transfer) == [
        (LedgerKind.AUTORISATION, 4860, "AUTH-1"),
        (LedgerKind.ANNULATION, 4860, "AUTH-1"),
    ]


def test_void_without_authorization_is_refused(session, transfer, paypal):
    with pytest.raises(service.PaymentError):
        service.void(session, transfer)

    assert paypal.calls == []


def test_pay_out_sends_the_price_to_the_seller_and_3_percent_to_the_organizer(session, transfer, paypal):
    service.create_order(session, transfer)
    service.authorize(session, transfer)
    service.capture(session, transfer)

    service.pay_out(session, transfer)

    assert [(i.recipient_email, i.amount_cents) for i in paypal.payout_items] == [
        ("vendeur@example.com", 4500),
        ("orga@example.com", 135),
    ]
    assert ledger_lines(session, transfer)[-2:] == [
        (LedgerKind.VERSEMENT_VENDEUR, 4500, "BATCH-1"),
        (LedgerKind.VERSEMENT_ORGANISATEUR, 135, "BATCH-1"),
    ]


def test_full_cycle_leaves_the_platform_its_5_percent(session, transfer, paypal):
    service.create_order(session, transfer)
    service.authorize(session, transfer)
    service.capture(session, transfer)
    service.pay_out(session, transfer)

    amounts = {kind: amount for kind, amount, _ in ledger_lines(session, transfer)}
    paid_out = amounts[LedgerKind.VERSEMENT_VENDEUR] + amounts[LedgerKind.VERSEMENT_ORGANISATEUR]
    assert amounts[LedgerKind.CAPTURE] - paid_out == 225


def test_pay_out_before_capture_is_refused(session, transfer, paypal):
    service.create_order(session, transfer)
    service.authorize(session, transfer)

    with pytest.raises(service.PaymentError):
        service.pay_out(session, transfer)

    assert "send_payouts" not in paypal.operations()


def test_pay_out_never_exceeds_what_was_captured(session, transfer, paypal):
    service.create_order(session, transfer)
    service.authorize(session, transfer)
    service.capture(session, transfer)
    # Le prix de l'annonce change après l'encaissement : on ne verse pas plus que ce qu'on a reçu.
    listing = session.get(Listing, transfer.listing_id)
    listing.price_cents = 9000
    session.add(listing)
    session.commit()

    with pytest.raises(service.PaymentError):
        service.pay_out(session, transfer)

    assert "send_payouts" not in paypal.operations()


def test_pay_out_skips_the_organizer_when_their_share_is_zero(session, transfer, race, paypal):
    race.organizer_fee_bps = 0
    session.add(race)
    session.commit()
    service.create_order(session, transfer)
    service.authorize(session, transfer)
    service.capture(session, transfer)

    service.pay_out(session, transfer)

    assert [i.recipient_email for i in paypal.payout_items] == ["vendeur@example.com"]
    assert ledger.find(session, transfer.id, LedgerKind.VERSEMENT_ORGANISATEUR) is None


def test_denied_payout_batch_records_nothing(session, transfer, paypal):
    service.create_order(session, transfer)
    service.authorize(session, transfer)
    service.capture(session, transfer)
    paypal.batch_status = "DENIED"

    with pytest.raises(PayPalError):
        service.pay_out(session, transfer)

    assert ledger.find(session, transfer.id, LedgerKind.VERSEMENT_VENDEUR) is None


def test_replaying_every_step_calls_paypal_once_and_writes_one_line_each(session, transfer, paypal):
    service.create_order(session, transfer)
    for _ in range(2):
        service.authorize(session, transfer)
        service.capture(session, transfer)
        service.pay_out(session, transfer)

    assert paypal.operations() == ["create_order", "authorize_order", "capture_authorization", "send_payouts"]
    assert [kind for kind, _, _ in ledger_lines(session, transfer)] == [
        LedgerKind.AUTORISATION,
        LedgerKind.CAPTURE,
        LedgerKind.VERSEMENT_VENDEUR,
        LedgerKind.VERSEMENT_ORGANISATEUR,
    ]


def test_replaying_void_calls_paypal_once(session, transfer, paypal):
    service.create_order(session, transfer)
    service.authorize(session, transfer)
    service.void(session, transfer)
    service.void(session, transfer)

    assert paypal.operations().count("void_authorization") == 1
    assert len(ledger_lines(session, transfer)) == 2


def test_each_step_has_its_own_stable_idempotency_key(session, transfer, paypal):
    service.create_order(session, transfer)
    session.expire_all()  # relit le transfert depuis la base, comme le ferait un nouvel essai
    service.create_order(session, session.get(Transfer, transfer.id))
    service.authorize(session, transfer)
    service.capture(session, transfer)
    service.pay_out(session, transfer)

    reference = service.transfer_reference(transfer)
    assert paypal.calls == [
        ("create_order", f"{reference}-order"),
        ("create_order", f"{reference}-order"),
        ("authorize_order", f"{reference}-authorize"),
        ("capture_authorization", f"{reference}-capture"),
        ("send_payouts", f"{reference}-payout"),
    ]
    assert reference.startswith(f"relais-{transfer.id}-")


def test_two_transfers_never_share_a_key(session, transfer):
    other = Transfer(listing_id=transfer.listing_id, state=TransferState.RESERVE)
    session.add(other)
    session.commit()

    assert service.transfer_reference(other) != service.transfer_reference(transfer)
