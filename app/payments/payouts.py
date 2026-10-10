"""Versements PayPal (API Payouts), via httpx : le SDK serveur ne couvre pas cette API.

Un lot (batch) regroupe plusieurs versements envoyés en un seul appel : ici la part du vendeur
et celle de l'organisateur. PayPal accepte le lot tout de suite, puis le traite en quelques
secondes : il faut relire le lot pour connaître le sort de chaque versement.
"""

from dataclasses import dataclass, field

import httpx

from app.payments.client import get_access_token, get_http_client, raise_for_paypal_error, send
from app.payments.money import CURRENCY, cents_to_paypal


@dataclass(frozen=True)
class PayoutItem:
    """Un versement à faire."""

    item_id: str  # notre identifiant du versement (sender_item_id chez PayPal)
    recipient_email: str  # adresse du compte PayPal qui reçoit l'argent
    amount_cents: int
    note: str  # message lu par le destinataire


@dataclass(frozen=True)
class PayoutBatch:
    id: str
    status: str  # PENDING ou PROCESSING, puis SUCCESS (ou DENIED, CANCELED)
    # Statut de chaque versement, par item_id. Vide tant que PayPal n'a pas commencé le lot.
    item_statuses: dict[str, str] = field(default_factory=dict)


def _batch(data: dict) -> PayoutBatch:
    header = data["batch_header"]
    return PayoutBatch(
        id=header["payout_batch_id"],
        status=header["batch_status"],
        item_statuses={
            item["payout_item"]["sender_item_id"]: item["transaction_status"] for item in data.get("items", [])
        },
    )


def _existing_batch_id(response: httpx.Response) -> str | None:
    """Si PayPal refuse le lot parce qu'il a déjà été envoyé, renvoie l'identifiant du lot d'origine."""
    if response.status_code != 400:
        return None
    try:
        details = response.json().get("details") or []
    except ValueError:
        return None
    for detail in details:
        if detail.get("field") == "SENDER_BATCH_ID":
            for link in detail.get("link") or []:
                if link.get("rel") == "self":
                    # Le lien se termine par l'identifiant : .../v1/payments/payouts/99XTTCCWYPMAJ
                    return link["href"].rstrip("/").rsplit("/", 1)[-1]
    return None


def send_payouts(
    items: list[PayoutItem],
    *,
    request_id: str,
    email_subject: str = "Relais : vous avez reçu un versement",
    http: httpx.Client | None = None,
) -> PayoutBatch:
    """Envoie un lot de versements depuis le solde PayPal de la plateforme.

    `request_id` sert deux fois : dans l'en-tête PayPal-Request-Id et comme sender_batch_id.
    PayPal ne traite jamais deux fois le même sender_batch_id (pendant 30 jours) : rejoué,
    l'appel ne verse rien de plus et renvoie le lot d'origine.
    """
    if not items:
        raise ValueError("Un lot de versements ne peut pas être vide")
    for item in items:
        if item.amount_cents <= 0:
            raise ValueError(f"Versement {item.item_id} : le montant doit être strictement positif")
    http = http or get_http_client()
    response = send(
        http,
        "POST",
        "/v1/payments/payouts",
        headers={
            "Authorization": f"Bearer {get_access_token(http)}",
            "PayPal-Request-Id": request_id,
        },
        json={
            "sender_batch_header": {"sender_batch_id": request_id, "email_subject": email_subject},
            "items": [
                {
                    "recipient_type": "EMAIL",
                    "receiver": item.recipient_email,
                    "amount": {"currency": CURRENCY, "value": cents_to_paypal(item.amount_cents)},
                    "note": item.note,
                    "sender_item_id": item.item_id,
                }
                for item in items
            ],
        },
    )
    existing_batch_id = _existing_batch_id(response)
    if existing_batch_id:
        return get_payout_batch(existing_batch_id, http=http)
    raise_for_paypal_error(response)
    return _batch(response.json())


def get_payout_batch(batch_id: str, *, http: httpx.Client | None = None) -> PayoutBatch:
    """Relit un lot pour savoir où en sont ses versements."""
    http = http or get_http_client()
    response = send(
        http,
        "GET",
        f"/v1/payments/payouts/{batch_id}",
        headers={"Authorization": f"Bearer {get_access_token(http)}"},
    )
    raise_for_paypal_error(response)
    return _batch(response.json())
