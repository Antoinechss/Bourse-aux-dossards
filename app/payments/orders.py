"""Commandes et autorisations PayPal, via le SDK serveur.

Cycle : créer la commande (intent AUTHORIZE) -> l'acheteur l'approuve -> autoriser (les fonds
sont bloqués, pas débités) -> capturer (débit), ou annuler (void : l'acheteur n'est jamais débité).

Chaque appel qui crée ou modifie quelque chose prend un request_id, envoyé dans l'en-tête
PayPal-Request-Id : rejoué avec la même clé, l'appel ne refait pas l'opération.
"""

import json
from dataclasses import dataclass

from paypalserversdk.exceptions.api_exception import ApiException
from paypalserversdk.models.amount_breakdown import AmountBreakdown
from paypalserversdk.models.amount_with_breakdown import AmountWithBreakdown
from paypalserversdk.models.capture_request import CaptureRequest
from paypalserversdk.models.checkout_payment_intent import CheckoutPaymentIntent
from paypalserversdk.models.item_category import ItemCategory
from paypalserversdk.models.item_request import ItemRequest
from paypalserversdk.models.money import Money
from paypalserversdk.models.order_request import OrderRequest
from paypalserversdk.models.payment_source import PaymentSource
from paypalserversdk.models.paypal_experience_user_action import PaypalExperienceUserAction
from paypalserversdk.models.paypal_wallet import PaypalWallet
from paypalserversdk.models.paypal_wallet_context_shipping_preference import (
    PaypalWalletContextShippingPreference,
)
from paypalserversdk.models.paypal_wallet_experience_context import PaypalWalletExperienceContext
from paypalserversdk.models.purchase_unit_request import PurchaseUnitRequest
from paypalserversdk.paypal_serversdk_client import PaypalServersdkClient

from app.payments.client import PayPalError, PayPalUnreachable, get_sdk_client
from app.payments.commission import Amounts
from app.payments.money import CURRENCY, cents_to_paypal, paypal_to_cents

# Demande à PayPal de renvoyer l'objet complet (montant, statut), pas seulement son identifiant.
FULL_RESPONSE = "return=representation"


@dataclass(frozen=True)
class Order:
    id: str
    status: str  # PAYER_ACTION_REQUIRED, puis APPROVED une fois que l'acheteur a approuvé
    approve_url: str | None  # page PayPal où l'acheteur approuve le paiement


@dataclass(frozen=True)
class Authorization:
    id: str
    status: str  # CREATED = fonds bloqués ; DENIED = refusée par PayPal
    amount_cents: int


@dataclass(frozen=True)
class Capture:
    id: str
    status: str  # COMPLETED = l'argent est sur le compte de la plateforme
    amount_cents: int


def _call(endpoint, options: dict) -> dict:
    """Appelle le SDK et renvoie le JSON de la réponse. Toute erreur PayPal devient une PayPalError."""
    try:
        response = endpoint(options)
    except ApiException as exc:
        # Le premier détail porte la cause précise ; le message général est souvent trop vague.
        detail = (getattr(exc, "details", None) or [None])[0]
        raise PayPalError(
            getattr(detail, "description", None) or getattr(exc, "message", None) or exc.reason,
            status_code=exc.response_code,
            name=getattr(exc, "name", None),
            issue=getattr(detail, "issue", None),
            debug_id=getattr(exc, "debug_id", None),
        ) from exc
    except OSError as exc:
        # Le SDK signale les pannes réseau (coupure, délai dépassé) par des sous-classes d'OSError.
        raise PayPalUnreachable(f"PayPal injoignable ({exc})") from exc
    # On lit le JSON brut plutôt que les objets du SDK : un champ absent y est simplement absent
    # du dictionnaire, alors que sur les objets du SDK l'attribut n'existe pas du tout.
    return json.loads(response.text) if response.text else {}


def _money(cents: int) -> Money:
    return Money(currency_code=CURRENCY, value=cents_to_paypal(cents))


def _order(data: dict) -> Order:
    approve_url = None
    for link in data.get("links", []):
        if link["rel"] in ("payer-action", "approve"):
            approve_url = link["href"]
    return Order(id=data["id"], status=data["status"], approve_url=approve_url)


def create_order(
    *,
    request_id: str,
    reference: str,
    description: str,
    amounts: Amounts,
    return_url: str | None = None,
    cancel_url: str | None = None,
    client: PaypalServersdkClient | None = None,
) -> Order:
    """Crée une commande en intent AUTHORIZE pour le total dû par l'acheteur.

    `reference` nous revient dans les réponses et les webhooks (custom_id) : elle permet de
    retrouver le transfert. Les URL de retour ne servent que sans le bouton PayPal, quand
    l'acheteur approuve en suivant le lien `approve_url`.
    """
    client = client or get_sdk_client()
    description = description[:127]  # longueur maximale acceptée par PayPal
    # L'acheteur voit deux lignes sur la page PayPal : le dossard, puis les frais de service.
    items = [
        ItemRequest(
            name=description,
            quantity="1",
            unit_amount=_money(amounts.price_cents),
            category=ItemCategory.DIGITAL_GOODS,
        )
    ]
    if amounts.service_fee_cents > 0:
        items.append(
            ItemRequest(
                name="Frais de service",
                quantity="1",
                unit_amount=_money(amounts.service_fee_cents),
                category=ItemCategory.DIGITAL_GOODS,
            )
        )
    experience = {
        "brand_name": "Relais",
        "locale": "fr-FR",
        # Un dossard ne s'expédie pas : PayPal ne demande pas d'adresse de livraison.
        "shipping_preference": PaypalWalletContextShippingPreference.NO_SHIPPING,
        "user_action": PaypalExperienceUserAction.PAY_NOW,
    }
    if return_url and cancel_url:
        experience["return_url"] = return_url
        experience["cancel_url"] = cancel_url
    body = OrderRequest(
        intent=CheckoutPaymentIntent.AUTHORIZE,
        purchase_units=[
            PurchaseUnitRequest(
                reference_id=reference,
                custom_id=reference,
                description=description,
                amount=AmountWithBreakdown(
                    currency_code=CURRENCY,
                    value=cents_to_paypal(amounts.total_cents),
                    breakdown=AmountBreakdown(item_total=_money(amounts.total_cents)),
                ),
                items=items,
            )
        ],
        payment_source=PaymentSource(
            paypal=PaypalWallet(experience_context=PaypalWalletExperienceContext(**experience))
        ),
    )
    data = _call(
        client.orders.create_order,
        {"body": body, "paypal_request_id": request_id, "prefer": FULL_RESPONSE},
    )
    return _order(data)


def get_order(order_id: str, *, client: PaypalServersdkClient | None = None) -> Order:
    """Relit une commande, par exemple pour savoir si l'acheteur l'a approuvée."""
    client = client or get_sdk_client()
    return _order(_call(client.orders.get_order, {"id": order_id}))


def authorize_order(
    order_id: str, *, request_id: str, client: PaypalServersdkClient | None = None
) -> Authorization:
    """Bloque les fonds d'une commande approuvée. L'acheteur n'est pas encore débité."""
    client = client or get_sdk_client()
    data = _call(
        client.orders.authorize_order,
        {"id": order_id, "paypal_request_id": request_id, "prefer": FULL_RESPONSE},
    )
    authorization = data["purchase_units"][0]["payments"]["authorizations"][0]
    return Authorization(
        id=authorization["id"],
        status=authorization["status"],
        amount_cents=paypal_to_cents(authorization["amount"]["value"]),
    )


def capture_authorization(
    authorization_id: str, *, request_id: str, client: PaypalServersdkClient | None = None
) -> Capture:
    """Encaisse la totalité d'une autorisation : c'est ici que l'acheteur est débité."""
    client = client or get_sdk_client()
    data = _call(
        client.payments.capture_authorized_payment,
        {
            "authorization_id": authorization_id,
            "paypal_request_id": request_id,
            "prefer": FULL_RESPONSE,
            # Sans montant, PayPal capture tout ; final_capture libère le reliquat éventuel.
            "body": CaptureRequest(final_capture=True),
        },
    )
    return Capture(id=data["id"], status=data["status"], amount_cents=paypal_to_cents(data["amount"]["value"]))


def void_authorization(
    authorization_id: str, *, request_id: str, client: PaypalServersdkClient | None = None
) -> None:
    """Annule une autorisation : les fonds sont libérés, l'acheteur n'a jamais été débité.

    PayPal répond sans contenu quand l'annulation réussit, et par une erreur sinon.
    """
    client = client or get_sdk_client()
    _call(
        client.payments.void_payment,
        {"authorization_id": authorization_id, "paypal_request_id": request_id},
    )
