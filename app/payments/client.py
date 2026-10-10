"""Accès à PayPal sandbox : client du SDK (commandes, autorisations) et httpx (Payouts).

Les deux remontent la même erreur, PayPalError : le reste de l'application n'a pas à savoir
quelle bibliothèque a fait l'appel.
"""

from functools import lru_cache

import httpx
from paypalserversdk.configuration import Environment
from paypalserversdk.http.auth.o_auth_2 import ClientCredentialsAuthCredentials
from paypalserversdk.paypal_serversdk_client import PaypalServersdkClient

from app.config import settings

SANDBOX_URL = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    """PayPal a refusé un appel, ou a répondu par un statut inattendu."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        name: str | None = None,
        issue: str | None = None,
        debug_id: str | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.name = name  # famille d'erreur, ex. UNPROCESSABLE_ENTITY
        self.issue = issue  # cause précise, ex. ORDER_NOT_APPROVED
        self.debug_id = debug_id  # à donner au support PayPal
        codes = " / ".join(str(part) for part in (status_code, name, issue) if part)
        suffix = f" (debug_id {debug_id})" if debug_id else ""
        super().__init__(f"PayPal {codes} : {message}{suffix}" if codes else f"PayPal : {message}{suffix}")


class PayPalUnreachable(PayPalError):
    """PayPal n'a pas répondu (coupure réseau, délai dépassé) : on ignore si l'appel a abouti.

    Rejouer l'appel est sans danger : la clé PayPal-Request-Id empêche de refaire l'opération.
    """


def _check_settings() -> None:
    # Règle du projet : sandbox uniquement. Mieux vaut refuser que risquer un vrai paiement.
    if settings.paypal_base_url != SANDBOX_URL:
        raise RuntimeError(f"PAYPAL_BASE_URL doit valoir {SANDBOX_URL} : Relais ne parle qu'à la sandbox")
    if not settings.paypal_client_id or not settings.paypal_client_secret:
        raise RuntimeError("PAYPAL_CLIENT_ID et PAYPAL_CLIENT_SECRET doivent être renseignés dans .env")


@lru_cache
def get_sdk_client() -> PaypalServersdkClient:
    """Client du SDK PayPal, créé au premier appel. Il gère lui-même son jeton OAuth."""
    _check_settings()
    return PaypalServersdkClient(
        client_credentials_auth_credentials=ClientCredentialsAuthCredentials(
            o_auth_client_id=settings.paypal_client_id,
            o_auth_client_secret=settings.paypal_client_secret,
        ),
        environment=Environment.SANDBOX,
    )


@lru_cache
def get_http_client() -> httpx.Client:
    """Client httpx pour les API que le SDK ne couvre pas (Payouts)."""
    _check_settings()
    return httpx.Client(base_url=settings.paypal_base_url, timeout=30)


def send(http: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    """Envoie une requête httpx à PayPal. Une panne réseau devient une PayPalUnreachable."""
    try:
        return http.request(method, url, **kwargs)
    except httpx.TransportError as exc:
        raise PayPalUnreachable(f"PayPal injoignable ({exc})") from exc


def raise_for_paypal_error(response: httpx.Response) -> None:
    """Transforme une réponse d'erreur httpx en PayPalError. Ne fait rien si l'appel a réussi."""
    if response.is_success:
        return
    try:
        body = response.json()
    except ValueError:
        body = {}
    details = body.get("details") or []
    raise PayPalError(
        # Les erreurs OAuth n'ont pas la même forme que celles des autres API.
        body.get("message") or body.get("error_description") or response.text,
        status_code=response.status_code,
        name=body.get("name") or body.get("error"),
        issue=details[0].get("issue") if details else None,
        debug_id=body.get("debug_id"),
    )


def get_access_token(http: httpx.Client) -> str:
    """Demande un jeton OAuth à PayPal avec l'identifiant et le secret de l'application."""
    response = send(
        http,
        "POST",
        "/v1/oauth2/token",
        auth=(settings.paypal_client_id, settings.paypal_client_secret),
        data={"grant_type": "client_credentials"},
    )
    raise_for_paypal_error(response)
    return response.json()["access_token"]
