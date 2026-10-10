"""Paiement d'un transfert : relie PayPal, la base et le journal comptable.

Chaque fonction fait un appel PayPal, note l'identifiant PayPal sur le transfert et écrit le
mouvement au journal. Aucune ne change l'état du transfert : c'est le rôle de app/transfers/.

Toutes peuvent être rejouées sans danger : la clé PayPal-Request-Id est la même à chaque
essai, et une étape déjà inscrite au journal n'est pas refaite.
"""

from datetime import timezone

from sqlmodel import Session

from app.models import LedgerEntry, LedgerKind, Listing, Race, Transfer, utcnow
from app.payments import ledger, orders, payouts
from app.payments.client import PayPalError
from app.payments.commission import Amounts, compute_amounts


class PaymentError(Exception):
    """L'étape demandée est impossible là où en est le paiement (ex. capturer avant d'autoriser)."""


def transfer_reference(transfer: Transfer) -> str:
    """Référence du transfert chez PayPal : base de toutes ses clés d'idempotence.

    Le numéro seul ne suffit pas : si la base locale est recréée, un nouveau transfert n°1
    réutiliserait les clés de l'ancien, et PayPal lui renverrait les anciennes réponses.
    """
    created = transfer.created_at
    if created.tzinfo is not None:
        created = created.astimezone(timezone.utc)
    return f"relais-{transfer.id}-{created:%Y%m%d%H%M%S}"


def _listing_and_race(session: Session, transfer: Transfer) -> tuple[Listing, Race]:
    listing = session.get(Listing, transfer.listing_id)
    return listing, session.get(Race, listing.race_id)


def amounts_for(session: Session, transfer: Transfer) -> Amounts:
    """Prix, frais et total du transfert, d'après l'annonce et la commission de la course."""
    listing, race = _listing_and_race(session, transfer)
    return compute_amounts(listing.price_cents, race.platform_fee_bps, race.organizer_fee_bps)


def create_order(
    session: Session, transfer: Transfer, *, return_url: str | None = None, cancel_url: str | None = None
) -> orders.Order:
    """Crée la commande PayPal du transfert et retient son identifiant.

    Aucun mouvement d'argent ici, donc pas de ligne au journal : tant que l'acheteur n'a pas
    approuvé puis été autorisé, rien n'est bloqué.
    """
    if transfer.paypal_authorization_id:
        raise PaymentError("Ce transfert a déjà une autorisation de paiement")
    listing, race = _listing_and_race(session, transfer)
    reference = transfer_reference(transfer)
    order = orders.create_order(
        request_id=f"{reference}-order",
        reference=reference,
        description=f"Dossard n°{listing.bib_number} — {race.name}",
        amounts=amounts_for(session, transfer),
        return_url=return_url,
        cancel_url=cancel_url,
    )
    transfer.paypal_order_id = order.id
    session.add(transfer)
    session.commit()
    return order


def authorize(session: Session, transfer: Transfer) -> LedgerEntry:
    """Bloque les fonds de l'acheteur, qui vient d'approuver la commande. Il n'est pas encore débité."""
    done = ledger.find(session, transfer.id, LedgerKind.AUTORISATION)
    if done:
        return done
    if not transfer.paypal_order_id:
        raise PaymentError("Aucune commande PayPal n'a été créée pour ce transfert")
    authorization = orders.authorize_order(
        transfer.paypal_order_id, request_id=f"{transfer_reference(transfer)}-authorize"
    )
    if authorization.status != "CREATED":
        raise PayPalError(f"Autorisation {authorization.id} en statut {authorization.status} : fonds non bloqués")
    transfer.paypal_authorization_id = authorization.id
    transfer.authorized_at = utcnow()
    session.add(transfer)
    entry = ledger.record(
        session, transfer.id, LedgerKind.AUTORISATION, authorization.amount_cents, authorization.id
    )
    session.commit()
    return entry


def capture(session: Session, transfer: Transfer) -> LedgerEntry:
    """Encaisse l'autorisation : l'acheteur est débité, l'argent arrive sur le compte de la plateforme."""
    done = ledger.find(session, transfer.id, LedgerKind.CAPTURE)
    if done:
        return done
    if not transfer.paypal_authorization_id:
        raise PaymentError("Ce transfert n'a pas d'autorisation à capturer")
    captured = orders.capture_authorization(
        transfer.paypal_authorization_id, request_id=f"{transfer_reference(transfer)}-capture"
    )
    if captured.status != "COMPLETED":
        raise PayPalError(f"Capture {captured.id} en statut {captured.status} : argent pas encore reçu")
    entry = ledger.record(session, transfer.id, LedgerKind.CAPTURE, captured.amount_cents, captured.id)
    session.commit()
    return entry


def void(session: Session, transfer: Transfer) -> LedgerEntry:
    """Annule l'autorisation : les fonds sont libérés, l'acheteur n'a jamais été débité."""
    done = ledger.find(session, transfer.id, LedgerKind.ANNULATION)
    if done:
        return done
    authorized = ledger.find(session, transfer.id, LedgerKind.AUTORISATION)
    if not transfer.paypal_authorization_id or not authorized:
        raise PaymentError("Ce transfert n'a pas d'autorisation à annuler")
    orders.void_authorization(
        transfer.paypal_authorization_id, request_id=f"{transfer_reference(transfer)}-void"
    )
    entry = ledger.record(
        session, transfer.id, LedgerKind.ANNULATION, authorized.amount_cents, transfer.paypal_authorization_id
    )
    session.commit()
    return entry


def pay_out(session: Session, transfer: Transfer) -> list[LedgerEntry]:
    """Verse le prix au vendeur et sa part des frais à l'organisateur, en un seul lot Payouts.

    La part de la plateforme reste sur son compte. Les lignes du journal portent l'identifiant
    du lot : PayPal le traite en quelques secondes, payouts.get_payout_batch dit où il en est.
    """
    payout_kinds = (LedgerKind.VERSEMENT_VENDEUR, LedgerKind.VERSEMENT_ORGANISATEUR)
    done = [entry for entry in ledger.entries(session, transfer.id) if entry.kind in payout_kinds]
    if done:
        return done
    captured = ledger.find(session, transfer.id, LedgerKind.CAPTURE)
    if not captured:
        raise PaymentError("Rien n'a été encaissé pour ce transfert : aucun versement possible")

    listing, race = _listing_and_race(session, transfer)
    amounts = amounts_for(session, transfer)
    reference = transfer_reference(transfer)
    bib = f"dossard n°{listing.bib_number} — {race.name}"
    planned = [
        (
            LedgerKind.VERSEMENT_VENDEUR,
            payouts.PayoutItem(
                item_id=f"{reference}-vendeur",
                recipient_email=listing.seller_email,
                amount_cents=amounts.price_cents,
                note=f"Vente de votre {bib}",
            ),
        )
    ]
    # Une course peut avoir fixé la part organisateur à zéro : PayPal refuse un versement nul.
    if amounts.organizer_fee_cents > 0:
        planned.append(
            (
                LedgerKind.VERSEMENT_ORGANISATEUR,
                payouts.PayoutItem(
                    item_id=f"{reference}-organisateur",
                    recipient_email=race.organizer_email,
                    amount_cents=amounts.organizer_fee_cents,
                    note=f"Votre part des frais de service, {bib}",
                ),
            )
        )
    total_cents = sum(item.amount_cents for _, item in planned)
    if total_cents > captured.amount_cents:
        raise PaymentError(
            f"Versements prévus ({total_cents} centimes) supérieurs au montant encaissé ({captured.amount_cents})"
        )

    batch = payouts.send_payouts([item for _, item in planned], request_id=f"{reference}-payout")
    if batch.status in ("DENIED", "CANCELED"):
        raise PayPalError(f"Lot de versements {batch.id} en statut {batch.status}")
    recorded = [ledger.record(session, transfer.id, kind, item.amount_cents, batch.id) for kind, item in planned]
    session.commit()
    return recorded
