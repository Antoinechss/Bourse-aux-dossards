"""Suite d'un transfert vérifié : changement de nom, encaissement, versement.

Les pages enregistrent des décisions (payer, déposer un justificatif). Les étapes d'argent qui en
découlent sont exécutées ici, par le ménage périodique (housekeeping) ou à l'arrivée d'un webhook
PayPal, et reprises au passage suivant si PayPal a échoué en route.
"""

import logging
import threading
from datetime import datetime

from sqlmodel import Session, col, select

from app.marketplace import service as market
from app.models import LedgerKind, Listing, Race, Transfer, WaitlistEntry
from app.notifications import notify
from app.payments import ledger, payouts, service
from app.registry_mock.registry import change_name
from app.templating import euros
from app.transfers.state import TransferEvent, TransferState, next_state

logger = logging.getLogger(__name__)

# Le ménage périodique et les webhooks tournent dans des fils différents : ce verrou empêche
# qu'ils fassent avancer le même transfert au même moment.
LOCK = threading.Lock()

# États dans lesquels il reste une étape automatique à faire.
TO_ADVANCE = (TransferState.VERIFIE, TransferState.NOM_CHANGE, TransferState.ENCAISSE)


def _move(session: Session, transfer: Transfer, event: TransferEvent) -> None:
    transfer.state = next_state(transfer.state, event)
    session.add(transfer)
    session.commit()


def advance(session: Session, transfer: Transfer) -> None:
    """Fait avancer un transfert vérifié aussi loin que possible. Ne fait rien dans les autres états.

    Chaque étape est enregistrée avant la suivante : si PayPal échoue en route, un nouvel appel
    reprend là où le transfert s'est arrêté, sans rien refaire.
    """
    if transfer.state not in TO_ADVANCE:
        return
    listing = session.get(Listing, transfer.listing_id)
    race = session.get(Race, listing.race_id)
    buyer = session.get(WaitlistEntry, transfer.buyer_id)

    if transfer.state is TransferState.VERIFIE:
        # Le registre est celui de l'organisateur : c'est ce changement de nom qui débloque l'argent.
        if change_name(session, race.id, listing.bib_number, buyer.first_name, buyer.last_name) is None:
            raise LookupError(f"Dossard n°{listing.bib_number} introuvable dans le registre de « {race.name} »")
        _move(session, transfer, TransferEvent.CHANGER_NOM)

    if transfer.state is TransferState.NOM_CHANGE:
        captured = service.capture(session, transfer)
        notify(
            session,
            buyer.email,
            f"Le dossard n°{listing.bib_number} est à votre nom — {race.name}",
            f"Votre paiement de {euros(captured.amount_cents)} € a été encaissé. Bonne course !",
            link=f"/runner/transfers/{transfer.id}",
        )
        _move(session, transfer, TransferEvent.ENCAISSER)

    if transfer.state is TransferState.ENCAISSE:
        # Le lot part chez PayPal. Le transfert ne passe à VERSE qu'une fois le lot confirmé.
        service.pay_out(session, transfer)


def check_payout(session: Session, transfer: Transfer) -> bool:
    """Relit le lot de versements chez PayPal et passe le transfert à VERSE si tout est arrivé.

    Renvoie False s'il n'y avait rien à confirmer, ou pas encore.
    """
    sent = ledger.find(session, transfer.id, LedgerKind.VERSEMENT_VENDEUR)
    if transfer.state is not TransferState.ENCAISSE or sent is None:
        return False
    batch = payouts.get_payout_batch(sent.paypal_id)
    # Un lot « réussi » peut contenir un versement non réclamé ou renvoyé : on exige que chacun soit arrivé.
    if batch.status != "SUCCESS" or any(status != "SUCCESS" for status in batch.item_statuses.values()):
        return False

    listing = session.get(Listing, transfer.listing_id)
    race = session.get(Race, listing.race_id)
    notify(
        session,
        listing.seller_email,
        f"Vous avez été payé — dossard n°{listing.bib_number}",
        f"{euros(listing.price_cents)} € ont été versés sur votre compte PayPal pour la vente de votre "
        f"dossard ({race.name}).",
        link=f"/runner/listings/{listing.id}",
    )
    _move(session, transfer, TransferEvent.VERSER)
    return True


def housekeeping(session: Session, now: datetime | None = None) -> None:
    """Le ménage périodique : offres expirées, transferts à faire avancer, versements à confirmer."""
    with LOCK:
        market.expire_offers(session, now)
        for transfer in session.exec(select(Transfer).where(col(Transfer.state).in_(TO_ADVANCE))).all():
            try:
                advance(session, transfer)
                check_payout(session, transfer)
            except Exception:
                # Un transfert en échec ne bloque pas les autres : il sera repris au prochain passage.
                session.rollback()
                logger.exception("Transfert %s : étape en échec, reprise au prochain passage", transfer.id)
