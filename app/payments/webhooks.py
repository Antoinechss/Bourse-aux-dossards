"""Webhooks PayPal : vérification de la signature, avant tout traitement."""

import json
from collections.abc import Mapping

import httpx

from app.config import settings
from app.payments.client import get_access_token, get_http_client, raise_for_paypal_error, send

# En-tête HTTP du webhook -> champ attendu par l'API de vérification de PayPal.
SIGNATURE_HEADERS = {
    "paypal-auth-algo": "auth_algo",
    "paypal-cert-url": "cert_url",
    "paypal-transmission-id": "transmission_id",
    "paypal-transmission-sig": "transmission_sig",
    "paypal-transmission-time": "transmission_time",
}


def verify_signature(headers: Mapping[str, str], raw_body: bytes, *, http: httpx.Client | None = None) -> bool:
    """Demande à PayPal si ce webhook vient bien de lui et n'a pas été modifié en route.

    Renvoie False si un en-tête de signature manque ou si PayPal ne reconnaît pas la signature.
    Lève PayPalError si PayPal n'a pas répondu : on ne sait alors pas, et rien ne doit être traité.
    """
    if not settings.paypal_webhook_id:
        raise RuntimeError("PAYPAL_WEBHOOK_ID doit être renseigné dans .env pour vérifier les webhooks")
    fields = {field: headers.get(header) for header, field in SIGNATURE_HEADERS.items()}
    if not all(fields.values()):
        return False
    fields["webhook_id"] = settings.paypal_webhook_id
    # L'événement est inséré tel qu'il a été reçu, octet pour octet : c'est ce texte que PayPal a signé.
    # Le réécrire avec json.dumps (espaces, ordre des clés, accents) ferait échouer la vérification.
    payload = json.dumps(fields)[:-1] + ', "webhook_event": ' + raw_body.decode("utf-8") + "}"
    http = http or get_http_client()
    response = send(
        http,
        "POST",
        "/v1/notifications/verify-webhook-signature",
        headers={"Authorization": f"Bearer {get_access_token(http)}", "Content-Type": "application/json"},
        content=payload.encode("utf-8"),
    )
    raise_for_paypal_error(response)
    return response.json().get("verification_status") == "SUCCESS"
