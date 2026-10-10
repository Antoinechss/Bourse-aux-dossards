"""Tests de la page d'achat (app/routers/runner.py) : PayPal est remplacé par un faux."""

from datetime import timedelta

import pytest
from paypal_fakes import FakePayPal, reserved_transfer

from app.models import LedgerKind, Transfer, utcnow
from app.payments import ledger
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


def set_state(session, transfer, state: TransferState) -> None:
    transfer.state = state
    session.add(transfer)
    session.commit()


def url(transfer, suffix: str) -> str:
    return f"/runner/transfers/{transfer.id}/{suffix}"


def test_checkout_page_shows_price_fee_and_total(client, transfer):
    response = client.get(url(transfer, "checkout"))

    assert response.status_code == 200
    assert "Dossard n°42 — Trail des Crêtes" in response.text
    assert "Bonjour Marie" in response.text
    for amount in ("45,00 €", "3,60 €", "48,60 €"):
        assert amount in response.text


def test_checkout_page_offers_the_paypal_button_while_the_offer_is_open(client, transfer):
    response = client.get(url(transfer, "checkout"))

    assert "<paypal-button" in response.text
    assert f'data-transfer-id="{transfer.id}"' in response.text
    assert "https://www.sandbox.paypal.com/web-sdk/v6/core" in response.text


def test_checkout_page_never_exposes_the_paypal_secret(client, transfer, monkeypatch):
    from app.routers import runner

    fake_settings = type("S", (), {"paypal_client_id": "ID-PUBLIC", "paypal_client_secret": "SECRET-A-CACHER"})
    monkeypatch.setattr(runner, "settings", fake_settings)

    response = client.get(url(transfer, "checkout"))

    assert 'data-client-id="ID-PUBLIC"' in response.text
    assert "SECRET-A-CACHER" not in response.text


def test_unknown_transfer_is_404(client):
    assert client.get("/runner/transfers/999/checkout").status_code == 404


def test_transfer_not_yet_offered_to_anyone_is_404(client, session, transfer):
    listed = Transfer(listing_id=transfer.listing_id)  # EN_VENTE, sans acheteur
    session.add(listed)
    session.commit()

    assert client.get(url(listed, "checkout")).status_code == 404
    assert client.post(url(listed, "order")).status_code == 404


def test_create_order_returns_the_paypal_order_id(client, session, transfer, paypal):
    response = client.post(url(transfer, "order"))

    assert response.status_code == 200
    assert response.json() == {"id": "ORDER-1"}
    assert session.get(Transfer, transfer.id).paypal_order_id == "ORDER-1"
    # Le montant vient du serveur : prix + frais de service.
    assert paypal.order_request["amounts"].total_cents == 4860


def test_authorize_blocks_the_funds_and_moves_the_transfer_to_autorise(client, session, transfer, paypal):
    client.post(url(transfer, "order"))

    response = client.post(url(transfer, "authorize"))

    assert response.status_code == 200
    assert response.json() == {"state": "AUTORISE"}
    assert session.get(Transfer, transfer.id).state is TransferState.AUTORISE
    entry = ledger.find(session, transfer.id, LedgerKind.AUTORISATION)
    assert (entry.amount_cents, entry.paypal_id) == (4860, "AUTH-1")


def test_authorize_twice_does_nothing_more(client, session, transfer, paypal):
    client.post(url(transfer, "order"))
    client.post(url(transfer, "authorize"))

    response = client.post(url(transfer, "authorize"))

    assert response.status_code == 200
    assert paypal.operations().count("authorize_order") == 1
    assert len(ledger.entries(session, transfer.id)) == 1


def test_authorize_before_any_order_is_refused(client, session, transfer, paypal):
    response = client.post(url(transfer, "authorize"))

    assert response.status_code == 409
    assert session.get(Transfer, transfer.id).state is TransferState.RESERVE


def test_paypal_refusal_leaves_the_transfer_untouched(client, session, transfer, paypal):
    client.post(url(transfer, "order"))
    paypal.error = PayPalError("Payer has not yet approved the Order", status_code=422, issue="ORDER_NOT_APPROVED")

    response = client.post(url(transfer, "authorize"))

    assert response.status_code == 502
    # L'acheteur lit un message simple, pas le détail technique de PayPal.
    assert "ORDER_NOT_APPROVED" not in response.text
    assert "Rien n'a été débité" in response.json()["detail"]
    assert session.get(Transfer, transfer.id).state is TransferState.RESERVE
    assert ledger.entries(session, transfer.id) == []


def test_paypal_refusal_on_order_creation_is_reported(client, transfer, paypal):
    paypal.error = PayPalError("Internal error", status_code=500)

    assert client.post(url(transfer, "order")).status_code == 502


def test_expired_offer_cannot_be_paid(client, session, transfer, paypal):
    transfer.offer_expires_at = utcnow() - timedelta(minutes=1)
    session.add(transfer)
    session.commit()

    page = client.get(url(transfer, "checkout"))

    assert "Cette offre a expiré" in page.text
    assert "<paypal-button" not in page.text
    assert client.post(url(transfer, "order")).status_code == 409
    assert client.post(url(transfer, "authorize")).status_code == 409
    assert paypal.calls == []


def test_offer_still_open_before_its_deadline(client, session, transfer):
    transfer.offer_expires_at = utcnow() + timedelta(hours=1)
    session.add(transfer)
    session.commit()

    assert "<paypal-button" in client.get(url(transfer, "checkout")).text


@pytest.mark.parametrize(
    "state, expected",
    [
        (TransferState.AUTORISE, "Paiement autorisé"),
        (TransferState.VERIFIE, "Paiement autorisé"),
        (TransferState.NOM_CHANGE, "Paiement autorisé"),
        (TransferState.ENCAISSE, "Paiement encaissé"),
        (TransferState.VERSE, "Paiement encaissé"),
        (TransferState.REFUSE, "vous n'avez pas été débité"),
        (TransferState.OFFRE_EXPIREE, "Cette offre a expiré"),
    ],
)
def test_checkout_page_follows_the_transfer_state_and_hides_the_button(client, session, transfer, state, expected):
    set_state(session, transfer, state)

    response = client.get(url(transfer, "checkout"))

    assert expected in response.text
    assert "<paypal-button" not in response.text


@pytest.mark.parametrize("state", [TransferState.VERIFIE, TransferState.ENCAISSE, TransferState.REFUSE])
def test_no_new_order_once_the_offer_is_settled(client, session, transfer, paypal, state):
    set_state(session, transfer, state)

    assert client.post(url(transfer, "order")).status_code == 409
    assert paypal.calls == []
