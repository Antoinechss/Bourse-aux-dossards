"""Tests de app/payments/orders.py sans réseau : un faux client remplace le SDK PayPal."""

import json
from types import SimpleNamespace

import pytest
from paypalserversdk.api_helper import APIHelper
from paypalserversdk.exceptions.error_exception import ErrorException

from app.payments import orders
from app.payments.client import PayPalError, PayPalUnreachable
from app.payments.commission import compute_amounts

AMOUNTS = compute_amounts(4500, platform_fee_bps=500, organizer_fee_bps=300)


class FakeEndpoint:
    """Remplace une méthode du SDK : retient les options reçues, renvoie la réponse préparée."""

    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = []

    def __call__(self, options):
        self.calls.append(options)
        if self.error:
            raise self.error
        return SimpleNamespace(text=json.dumps(self.response) if self.response is not None else "")


def fake_client(endpoint: FakeEndpoint):
    # Chaque test n'appelle qu'une méthode : la même fausse méthode les remplace toutes.
    controller = SimpleNamespace(
        create_order=endpoint,
        get_order=endpoint,
        authorize_order=endpoint,
        capture_authorized_payment=endpoint,
        void_payment=endpoint,
    )
    return SimpleNamespace(orders=controller, payments=controller)


def sent_body(endpoint: FakeEndpoint) -> dict:
    """Le JSON que le SDK aurait envoyé à PayPal."""
    return json.loads(APIHelper.json_serialize(endpoint.calls[0]["body"]))


# Réponse réelle de la sandbox à une création de commande, raccourcie.
CREATED_ORDER = {
    "id": "39B03304R8947231X",
    "intent": "AUTHORIZE",
    "status": "PAYER_ACTION_REQUIRED",
    "links": [
        {"href": "https://api.sandbox.paypal.com/v2/checkout/orders/39B03304R8947231X", "rel": "self", "method": "GET"},
        {
            "href": "https://www.sandbox.paypal.com/checkoutnow?token=39B03304R8947231X",
            "rel": "payer-action",
            "method": "GET",
        },
    ],
}


def create(endpoint, **kwargs):
    return orders.create_order(
        request_id="relais-12-order",
        reference="relais-12",
        description="Dossard n°42 — Trail des Crêtes",
        amounts=kwargs.pop("amounts", AMOUNTS),
        client=fake_client(endpoint),
        **kwargs,
    )


def test_create_order_uses_authorize_intent_and_the_buyer_total():
    endpoint = FakeEndpoint(CREATED_ORDER)

    create(endpoint)

    body = sent_body(endpoint)
    unit = body["purchase_units"][0]
    assert body["intent"] == "AUTHORIZE"
    assert unit["amount"] == {
        "currency_code": "EUR",
        "value": "48.60",
        "breakdown": {"item_total": {"currency_code": "EUR", "value": "48.60"}},
    }
    assert unit["custom_id"] == "relais-12"


def test_create_order_shows_the_bib_and_the_service_fee_as_two_lines():
    endpoint = FakeEndpoint(CREATED_ORDER)

    create(endpoint)

    items = sent_body(endpoint)["purchase_units"][0]["items"]
    assert [(item["name"], item["unit_amount"]["value"], item["quantity"]) for item in items] == [
        ("Dossard n°42 — Trail des Crêtes", "45.00", "1"),
        ("Frais de service", "3.60", "1"),
    ]


def test_create_order_without_commission_has_no_fee_line():
    endpoint = FakeEndpoint(CREATED_ORDER)

    create(endpoint, amounts=compute_amounts(4500, platform_fee_bps=0, organizer_fee_bps=0))

    unit = sent_body(endpoint)["purchase_units"][0]
    assert len(unit["items"]) == 1
    assert unit["amount"]["value"] == "45.00"


def test_create_order_sends_the_request_id_and_asks_for_no_shipping_address():
    endpoint = FakeEndpoint(CREATED_ORDER)

    create(endpoint)

    assert endpoint.calls[0]["paypal_request_id"] == "relais-12-order"
    context = sent_body(endpoint)["payment_source"]["paypal"]["experience_context"]
    assert context["shipping_preference"] == "NO_SHIPPING"
    assert "return_url" not in context


def test_create_order_passes_return_urls_when_given():
    endpoint = FakeEndpoint(CREATED_ORDER)

    create(endpoint, return_url="https://example.com/retour", cancel_url="https://example.com/annule")

    context = sent_body(endpoint)["payment_source"]["paypal"]["experience_context"]
    assert context["return_url"] == "https://example.com/retour"
    assert context["cancel_url"] == "https://example.com/annule"


def test_create_order_returns_id_status_and_approval_link():
    order = create(FakeEndpoint(CREATED_ORDER))

    assert order == orders.Order(
        id="39B03304R8947231X",
        status="PAYER_ACTION_REQUIRED",
        approve_url="https://www.sandbox.paypal.com/checkoutnow?token=39B03304R8947231X",
    )


def test_get_order_reads_the_status():
    endpoint = FakeEndpoint({"id": "39B03304R8947231X", "status": "APPROVED", "links": []})

    order = orders.get_order("39B03304R8947231X", client=fake_client(endpoint))

    assert endpoint.calls == [{"id": "39B03304R8947231X"}]
    assert order.status == "APPROVED"
    assert order.approve_url is None


def test_authorize_order_returns_the_authorization():
    endpoint = FakeEndpoint(
        {
            "id": "39B03304R8947231X",
            "status": "COMPLETED",
            "purchase_units": [
                {
                    "reference_id": "relais-12",
                    "payments": {
                        "authorizations": [
                            {
                                "id": "0AW2184448108334S",
                                "status": "CREATED",
                                "amount": {"currency_code": "EUR", "value": "48.60"},
                            }
                        ]
                    },
                }
            ],
        }
    )

    authorization = orders.authorize_order(
        "39B03304R8947231X", request_id="relais-12-authorize", client=fake_client(endpoint)
    )

    assert endpoint.calls[0]["id"] == "39B03304R8947231X"
    assert endpoint.calls[0]["paypal_request_id"] == "relais-12-authorize"
    assert authorization == orders.Authorization(id="0AW2184448108334S", status="CREATED", amount_cents=4860)


def test_capture_authorization_captures_everything():
    endpoint = FakeEndpoint(
        {
            "id": "2GG279541U471931P",
            "status": "COMPLETED",
            "amount": {"currency_code": "EUR", "value": "48.60"},
            "final_capture": True,
        }
    )

    capture = orders.capture_authorization(
        "0AW2184448108334S", request_id="relais-12-capture", client=fake_client(endpoint)
    )

    assert endpoint.calls[0]["authorization_id"] == "0AW2184448108334S"
    assert endpoint.calls[0]["paypal_request_id"] == "relais-12-capture"
    # Pas de montant dans la demande : PayPal capture la totalité de l'autorisation.
    assert sent_body(endpoint) == {"final_capture": True}
    assert capture == orders.Capture(id="2GG279541U471931P", status="COMPLETED", amount_cents=4860)


def test_void_authorization_accepts_an_empty_response():
    endpoint = FakeEndpoint(response=None)

    orders.void_authorization("0AW2184448108334S", request_id="relais-12-void", client=fake_client(endpoint))

    assert endpoint.calls == [{"authorization_id": "0AW2184448108334S", "paypal_request_id": "relais-12-void"}]


def test_paypal_refusal_becomes_a_paypal_error():
    # Corps d'erreur réel de la sandbox : autoriser une commande que l'acheteur n'a pas approuvée.
    refusal = ErrorException(
        "The requested action could not be performed.",
        SimpleNamespace(
            status_code=422,
            text=json.dumps(
                {
                    "name": "UNPROCESSABLE_ENTITY",
                    "message": "The requested action could not be performed, semantically incorrect, or failed business validation.",
                    "debug_id": "f241172fb1549",
                    "details": [
                        {
                            "issue": "ORDER_NOT_APPROVED",
                            "description": "Payer has not yet approved the Order for payment.",
                        }
                    ],
                }
            ),
        ),
    )

    with pytest.raises(PayPalError) as caught:
        orders.authorize_order("39B03304R8947231X", request_id="k", client=fake_client(FakeEndpoint(error=refusal)))

    error = caught.value
    assert (error.status_code, error.name, error.issue) == (422, "UNPROCESSABLE_ENTITY", "ORDER_NOT_APPROVED")
    assert error.debug_id == "f241172fb1549"
    assert "Payer has not yet approved" in str(error)


def test_network_failure_becomes_paypal_unreachable():
    # C'est l'erreur que lève réellement le SDK quand la connexion est coupée.
    requests = pytest.importorskip("requests")
    cut = requests.exceptions.ConnectionError("Connection reset by peer")

    with pytest.raises(PayPalUnreachable):
        orders.get_order("39B03304R8947231X", client=fake_client(FakeEndpoint(error=cut)))
