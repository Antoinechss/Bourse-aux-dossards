"""Tests de app/payments/payouts.py sans réseau : httpx.MockTransport joue le rôle de PayPal."""

import json

import httpx
import pytest

from app.payments import payouts
from app.payments.client import PayPalError, PayPalUnreachable

ITEMS = [
    payouts.PayoutItem(
        item_id="relais-12-vendeur",
        recipient_email="vendeur@example.com",
        amount_cents=4500,
        note="Vente de votre dossard n°42",
    ),
    payouts.PayoutItem(
        item_id="relais-12-organisateur",
        recipient_email="orga@example.com",
        amount_cents=135,
        note="Commission sur le dossard n°42",
    ),
]

# Réponses réelles de la sandbox, raccourcies.
BATCH_CREATED = {"batch_header": {"payout_batch_id": "XGBFSC2VDK2H8", "batch_status": "PENDING"}}
BATCH_DONE = {
    "batch_header": {"payout_batch_id": "XGBFSC2VDK2H8", "batch_status": "SUCCESS"},
    "items": [
        {"transaction_status": "SUCCESS", "payout_item": {"sender_item_id": "relais-12-vendeur"}},
        {"transaction_status": "SUCCESS", "payout_item": {"sender_item_id": "relais-12-organisateur"}},
    ],
}
ALREADY_SENT = {
    "name": "USER_BUSINESS_ERROR",
    "message": "User business error.",
    "debug_id": "f33838240a0da",
    "details": [
        {
            "field": "SENDER_BATCH_ID",
            "location": "body",
            "issue": "Batch with given sender_batch_id already exists",
            "link": [
                {"href": "https://api.sandbox.paypal.com/v1/payments/payouts/XGBFSC2VDK2H8", "rel": "self"}
            ],
        }
    ],
}


class FakePayPal:
    """Faux serveur PayPal : retient les requêtes reçues et répond ce qu'on lui a préparé."""

    def __init__(self, create_response: tuple[int, dict]):
        self.create_response = create_response
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "JETON-DE-TEST"})
        if request.method == "POST" and request.url.path == "/v1/payments/payouts":
            status, body = self.create_response
            return httpx.Response(status, json=body)
        if request.method == "GET" and request.url.path == "/v1/payments/payouts/XGBFSC2VDK2H8":
            return httpx.Response(200, json=BATCH_DONE)
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "message": "Not found"})

    def client(self) -> httpx.Client:
        return httpx.Client(base_url="https://paypal.test", transport=httpx.MockTransport(self.handle))

    def payout_request(self) -> httpx.Request:
        return next(r for r in self.requests if r.method == "POST" and r.url.path == "/v1/payments/payouts")


def test_send_payouts_pays_seller_and_organizer_in_one_batch():
    paypal = FakePayPal((201, BATCH_CREATED))

    batch = payouts.send_payouts(ITEMS, request_id="relais-12-payout", http=paypal.client())

    assert batch == payouts.PayoutBatch(id="XGBFSC2VDK2H8", status="PENDING")
    sent = json.loads(paypal.payout_request().content)
    assert [(i["receiver"], i["amount"], i["sender_item_id"]) for i in sent["items"]] == [
        ("vendeur@example.com", {"currency": "EUR", "value": "45.00"}, "relais-12-vendeur"),
        ("orga@example.com", {"currency": "EUR", "value": "1.35"}, "relais-12-organisateur"),
    ]
    assert all(i["recipient_type"] == "EMAIL" for i in sent["items"])


def test_send_payouts_is_idempotent_by_header_and_batch_id():
    paypal = FakePayPal((201, BATCH_CREATED))

    payouts.send_payouts(ITEMS, request_id="relais-12-payout", http=paypal.client())

    request = paypal.payout_request()
    assert request.headers["PayPal-Request-Id"] == "relais-12-payout"
    assert request.headers["Authorization"] == "Bearer JETON-DE-TEST"
    assert json.loads(request.content)["sender_batch_header"]["sender_batch_id"] == "relais-12-payout"


def test_replayed_batch_returns_the_original_instead_of_paying_twice():
    paypal = FakePayPal((400, ALREADY_SENT))

    batch = payouts.send_payouts(ITEMS, request_id="relais-12-payout", http=paypal.client())

    assert batch.id == "XGBFSC2VDK2H8"
    assert batch.status == "SUCCESS"


def test_get_payout_batch_reports_each_item_status():
    paypal = FakePayPal((201, BATCH_CREATED))

    batch = payouts.get_payout_batch("XGBFSC2VDK2H8", http=paypal.client())

    assert batch.status == "SUCCESS"
    assert batch.item_statuses == {"relais-12-vendeur": "SUCCESS", "relais-12-organisateur": "SUCCESS"}


def test_paypal_refusal_becomes_a_paypal_error():
    refusal = {
        "name": "INSUFFICIENT_FUNDS",
        "message": "Sender does not have sufficient funds. Please add funds and retry.",
        "debug_id": "abc123",
    }
    paypal = FakePayPal((422, refusal))

    with pytest.raises(PayPalError) as caught:
        payouts.send_payouts(ITEMS, request_id="relais-12-payout", http=paypal.client())

    assert (caught.value.status_code, caught.value.name) == (422, "INSUFFICIENT_FUNDS")
    assert caught.value.debug_id == "abc123"


def test_bad_credentials_become_a_paypal_error():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_client", "error_description": "Client Authentication failed"})

    http = httpx.Client(base_url="https://paypal.test", transport=httpx.MockTransport(handle))

    with pytest.raises(PayPalError) as caught:
        payouts.send_payouts(ITEMS, request_id="relais-12-payout", http=http)

    assert (caught.value.status_code, caught.value.name) == (401, "invalid_client")
    assert "Client Authentication failed" in str(caught.value)


def test_empty_batch_and_zero_amounts_are_rejected_before_calling_paypal():
    paypal = FakePayPal((201, BATCH_CREATED))
    free = payouts.PayoutItem(item_id="x", recipient_email="orga@example.com", amount_cents=0, note="")

    with pytest.raises(ValueError):
        payouts.send_payouts([], request_id="relais-12-payout", http=paypal.client())
    with pytest.raises(ValueError):
        payouts.send_payouts([free], request_id="relais-12-payout", http=paypal.client())

    assert paypal.requests == []


def test_network_failure_becomes_paypal_unreachable():
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection reset by peer", request=request)

    http = httpx.Client(base_url="https://paypal.test", transport=httpx.MockTransport(handle))

    with pytest.raises(PayPalUnreachable):
        payouts.get_payout_batch("XGBFSC2VDK2H8", http=http)
