"""Tests des webhooks PayPal : vérification de la signature, puis traitement des événements."""

import dataclasses
import json

import httpx
import pytest
from paypal_fakes import FakePayPal, reserved_transfer
from sqlmodel import select

from app.config import settings
from app.models import Notification, Registration, Transfer, WebhookEvent
from app.payments import service, webhooks
from app.payments.client import PayPalError
from app.routers import webhooks as webhooks_router
from app.transfers.state import TransferState

SIGNATURE = {
    "PAYPAL-AUTH-ALGO": "SHA256withRSA",
    "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1",
    "PAYPAL-TRANSMISSION-ID": "transmission-1",
    "PAYPAL-TRANSMISSION-SIG": "c2lnbmF0dXJl",
    "PAYPAL-TRANSMISSION-TIME": "2026-10-10T12:00:00Z",
}
# Un corps aux espaces et aux accents inhabituels : il doit repartir chez PayPal sans un octet de changé.
RAW_EVENT = '{"id":"WH-1",   "event_type":"PAYMENT.CAPTURE.COMPLETED","summary":"caf\\u00e9 payé 45 €"}'.encode()


# --- La vérification de signature ---


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(webhooks, "settings", dataclasses.replace(settings, paypal_webhook_id="WEBHOOK-ID"))


def fake_paypal_api(verification_status: str):
    """Un faux PayPal côté HTTP : donne un jeton, puis répond à la demande de vérification."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "JETON"})
        return httpx.Response(200, json={"verification_status": verification_status})

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://paypal.test"), requests


def test_verification_sends_the_event_untouched_with_its_signature(configured):
    http, requests = fake_paypal_api("SUCCESS")

    assert webhooks.verify_signature(httpx.Headers(SIGNATURE), RAW_EVENT, http=http) is True

    request = requests[-1]
    assert request.url.path == "/v1/notifications/verify-webhook-signature"
    assert request.headers["Authorization"] == "Bearer JETON"
    assert RAW_EVENT in request.content  # octet pour octet
    sent = json.loads(request.content)
    assert sent["webhook_id"] == "WEBHOOK-ID"
    assert sent["transmission_id"] == "transmission-1"
    assert sent["transmission_sig"] == "c2lnbmF0dXJl"
    assert sent["webhook_event"]["id"] == "WH-1"


def test_signature_rejected_by_paypal_is_invalid(configured):
    http, _ = fake_paypal_api("FAILURE")

    assert webhooks.verify_signature(httpx.Headers(SIGNATURE), RAW_EVENT, http=http) is False


def test_missing_signature_header_is_invalid_without_asking_paypal(configured):
    http, requests = fake_paypal_api("SUCCESS")
    incomplete = {name: value for name, value in SIGNATURE.items() if name != "PAYPAL-TRANSMISSION-SIG"}

    assert webhooks.verify_signature(httpx.Headers(incomplete), RAW_EVENT, http=http) is False
    assert requests == []


def test_verification_is_impossible_without_the_webhook_id(monkeypatch):
    monkeypatch.setattr(webhooks, "settings", dataclasses.replace(settings, paypal_webhook_id=""))

    with pytest.raises(RuntimeError, match="PAYPAL_WEBHOOK_ID"):
        webhooks.verify_signature(httpx.Headers(SIGNATURE), RAW_EVENT)


# --- Le traitement des événements ---


@pytest.fixture
def paypal(monkeypatch):
    fake = FakePayPal()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def signature(monkeypatch):
    """Remplace la vérification : signature["valid"] dit si elle réussit, signature["error"] la fait échouer."""
    state = {"valid": True, "error": None, "calls": 0}

    def fake_verify(headers, raw_body):
        state["calls"] += 1
        if state["error"]:
            raise state["error"]
        return state["valid"]

    monkeypatch.setattr(webhooks_router, "verify_signature", fake_verify)
    return state


@pytest.fixture
def transfer(session, race, paypal):
    """Le dossard 42 est au nom de l'acheteuse ; son paiement autorisé attend d'être encaissé."""
    session.add(Registration(race_id=race.id, bib_number=42, first_name="Jean", last_name="Dupont"))
    transfer = reserved_transfer(session, race)
    service.create_order(session, transfer)
    service.authorize(session, transfer)
    transfer.state = TransferState.NOM_CHANGE
    session.add(transfer)
    session.commit()
    return transfer


def capture_completed(transfer, event_id="WH-CAPTURE") -> dict:
    return {
        "id": event_id,
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"id": "CAPT-1", "custom_id": service.transfer_reference(transfer)},
    }


def payouts_succeeded(transfer, event_id="WH-PAYOUT") -> dict:
    sender = {"sender_batch_id": f"{service.transfer_reference(transfer)}-payout"}
    return {
        "id": event_id,
        "event_type": "PAYMENT.PAYOUTSBATCH.SUCCESS",
        "resource": {"batch_header": {"payout_batch_id": "BATCH-1", "sender_batch_header": sender}},
    }


def state_of(session, transfer) -> TransferState:
    return session.get(Transfer, transfer.id).state


def test_event_with_an_invalid_signature_is_refused_and_nothing_happens(client, session, transfer, paypal, signature):
    signature["valid"] = False
    calls = len(paypal.calls)

    response = client.post("/webhooks/paypal", json=capture_completed(transfer))

    assert response.status_code == 400
    assert state_of(session, transfer) is TransferState.NOM_CHANGE
    assert len(paypal.calls) == calls
    assert session.exec(select(WebhookEvent)).first() is None


@pytest.mark.parametrize("error", [RuntimeError("PAYPAL_WEBHOOK_ID manquant"), PayPalError("injoignable")])
def test_event_is_put_off_when_the_signature_cannot_be_checked(client, session, transfer, signature, error):
    signature["error"] = error

    response = client.post("/webhooks/paypal", json=capture_completed(transfer))

    assert response.status_code == 503  # PayPal renverra l'événement plus tard
    assert state_of(session, transfer) is TransferState.NOM_CHANGE


def test_unreadable_event_is_refused_before_any_verification(client, signature):
    response = client.post("/webhooks/paypal", content=b"pas du json")

    assert response.status_code == 400
    assert signature["calls"] == 0


def test_capture_completed_moves_the_transfer_on(client, session, transfer, paypal, signature):
    response = client.post("/webhooks/paypal", json=capture_completed(transfer))

    assert response.json() == {"status": "processed"}
    assert state_of(session, transfer) is TransferState.ENCAISSE
    assert paypal.operations()[-1] == "send_payouts"
    assert session.exec(select(WebhookEvent)).one().event_type == "PAYMENT.CAPTURE.COMPLETED"


def test_payouts_succeeded_marks_the_transfer_paid(client, session, transfer, signature):
    client.post("/webhooks/paypal", json=capture_completed(transfer))

    response = client.post("/webhooks/paypal", json=payouts_succeeded(transfer))

    assert response.json() == {"status": "processed"}
    assert state_of(session, transfer) is TransferState.VERSE


def test_event_received_twice_is_processed_once(client, session, transfer, signature):
    client.post("/webhooks/paypal", json=capture_completed(transfer))
    client.post("/webhooks/paypal", json=payouts_succeeded(transfer))

    response = client.post("/webhooks/paypal", json=payouts_succeeded(transfer))

    assert response.json() == {"status": "duplicate"}
    paid = select(Notification).where(Notification.recipient_email == "vendeur@example.com")
    assert len(session.exec(paid).all()) == 1


def test_event_we_do_not_handle_is_acknowledged_and_ignored(client, session, transfer, signature):
    response = client.post("/webhooks/paypal", json={"id": "WH-X", "event_type": "CUSTOMER.DISPUTE.CREATED"})

    assert response.json() == {"status": "ignored"}
    assert state_of(session, transfer) is TransferState.NOM_CHANGE


def test_event_about_a_transfer_from_another_database_is_ignored(client, session, transfer, signature):
    event = capture_completed(transfer)
    event["resource"]["custom_id"] = f"relais-{transfer.id}-20200101000000"  # même numéro, autre date de création

    response = client.post("/webhooks/paypal", json=event)

    assert response.json() == {"status": "ignored"}
    assert state_of(session, transfer) is TransferState.NOM_CHANGE


def test_failed_processing_asks_paypal_to_send_the_event_again(client, session, transfer, paypal, signature):
    paypal.error = PayPalError("panne simulée")

    assert client.post("/webhooks/paypal", json=capture_completed(transfer)).status_code == 500
    assert session.exec(select(WebhookEvent)).first() is None  # pas noté comme traité

    paypal.error = None

    assert client.post("/webhooks/paypal", json=capture_completed(transfer)).json() == {"status": "processed"}
    assert state_of(session, transfer) is TransferState.ENCAISSE
