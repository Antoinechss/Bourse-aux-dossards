"""Marketplace : annonces, liste d'attente et offres.

Un dossard mis en vente est proposé au premier coureur de la liste d'attente, qui a OFFER_DURATION
pour le payer. Passé ce délai, l'offre expire et le dossard est proposé au suivant.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlmodel import Session, col, select

from app.ai.verifier import NameMatch, compare_names
from app.models import Listing, Race, RaceRulesRow, Transfer, WaitlistEntry, utcnow
from app.notifications import notify
from app.registry_mock.registry import get_registration
from app.templating import euros, local_time
from app.transfers.state import TransferEvent, TransferState, next_state

OFFER_DURATION = timedelta(hours=48)

# Une tentative de transfert est terminée dans ces états ; dans tous les autres, elle est en cours.
FINISHED = (TransferState.VERSE, TransferState.REFUSE, TransferState.OFFRE_EXPIREE)
# Dans ces états, l'acheteur est reparti sans dossard : on peut lui en proposer un autre.
RELEASED = (TransferState.REFUSE, TransferState.OFFRE_EXPIREE)


class MarketError(Exception):
    """Demande refusée. Le message est en français et peut être montré tel quel au coureur."""


@dataclass(frozen=True)
class RaceMarket:
    """Où en est la bourse d'une course."""

    free: int  # dossards en vente que personne ne s'est encore vu proposer
    offered: int  # dossards proposés à un coureur, qui n'a pas encore payé
    waiting: int  # coureurs de la liste d'attente qui n'ont pas de dossard


def rules_for(session: Session, race_id: int) -> RaceRulesRow | None:
    return session.exec(select(RaceRulesRow).where(RaceRulesRow.race_id == race_id)).first()


def check_resale_open(race: Race, rules: RaceRulesRow | None, today: date) -> None:
    """Lève MarketError si la revente n'est pas, ou plus, ouverte pour cette course."""
    if rules and rules.transfer_allowed is False:
        raise MarketError("Le règlement de cette course interdit la revente des dossards.")
    if today > race.event_date:
        raise MarketError("Cette course a déjà eu lieu.")
    if rules and rules.transfer_deadline and today > rules.transfer_deadline:
        raise MarketError(f"Les transferts sont clos depuis le {rules.transfer_deadline:%d/%m/%Y}.")


def _race_transfers(session: Session, race_id: int, *states: TransferState) -> list[Transfer]:
    statement = (
        select(Transfer)
        .join(Listing, Listing.id == Transfer.listing_id)
        .where(Listing.race_id == race_id, col(Transfer.state).in_(states))
        .order_by(Transfer.id)
    )
    return list(session.exec(statement))


def _waiting(session: Session, race_id: int) -> list[WaitlistEntry]:
    """La file d'attente dans l'ordre d'inscription, sans ceux qui ont un dossard ou une offre en cours."""
    entries = session.exec(
        select(WaitlistEntry).where(WaitlistEntry.race_id == race_id).order_by(WaitlistEntry.created_at, WaitlistEntry.id)
    ).all()
    busy = set(
        session.exec(select(Transfer.buyer_id).where(col(Transfer.state).not_in(RELEASED), col(Transfer.buyer_id).is_not(None)))
    )
    return [entry for entry in entries if entry.id not in busy]


def race_market(session: Session, race_id: int) -> RaceMarket:
    return RaceMarket(
        free=len(_race_transfers(session, race_id, TransferState.EN_VENTE)),
        offered=len(_race_transfers(session, race_id, TransferState.RESERVE)),
        waiting=len(_waiting(session, race_id)),
    )


def next_in_line(session: Session, transfer: Transfer) -> WaitlistEntry | None:
    """Le premier coureur de la file à qui ce dossard peut être proposé."""
    listing = session.get(Listing, transfer.listing_id)
    already_offered = set(session.exec(select(Transfer.buyer_id).where(Transfer.listing_id == listing.id)))
    for entry in _waiting(session, listing.race_id):
        if entry.email.lower() == listing.seller_email.lower():
            continue  # on ne propose pas son propre dossard au vendeur
        if entry.id in already_offered:
            continue  # il a déjà laissé passer ce dossard
        return entry
    return None


def offer_next(session: Session, transfer: Transfer, now: datetime | None = None) -> WaitlistEntry | None:
    """Propose le dossard au suivant de la liste d'attente. S'il n'y a personne, il reste EN_VENTE."""
    listing = session.get(Listing, transfer.listing_id)
    race = session.get(Race, listing.race_id)
    try:
        check_resale_open(race, rules_for(session, race.id), date.today())
    except MarketError:
        return None
    entry = next_in_line(session, transfer)
    if entry is None:
        return None
    transfer.buyer_id = entry.id
    transfer.state = next_state(transfer.state, TransferEvent.RESERVER)
    transfer.offer_expires_at = (now or utcnow()) + OFFER_DURATION
    session.add(transfer)
    session.flush()  # attribue transfer.id, dont le lien du message a besoin
    notify(
        session,
        entry.email,
        f"Un dossard est disponible pour vous — {race.name}",
        f"Le dossard n°{listing.bib_number} vous est proposé à {euros(listing.price_cents)} €, hors frais de service. "
        f"Vous avez jusqu'au {local_time(transfer.offer_expires_at)} pour le payer : passé ce délai, "
        "il sera proposé au coureur suivant.",
        link=f"/runner/transfers/{transfer.id}",
    )
    session.commit()
    return entry


def offer_pending(session: Session, race_id: int, now: datetime | None = None) -> None:
    """Propose les dossards de cette course qui attendaient un acheteur, du plus ancien au plus récent."""
    for transfer in _race_transfers(session, race_id, TransferState.EN_VENTE):
        offer_next(session, transfer, now)


def join_waitlist(
    session: Session, race: Race, first_name: str, last_name: str, email: str, today: date | None = None
) -> WaitlistEntry:
    """Inscrit un coureur sur la liste d'attente. Si un dossard attendait preneur, il lui est proposé aussitôt."""
    check_resale_open(race, rules_for(session, race.id), today or date.today())
    already = session.exec(
        select(WaitlistEntry).where(WaitlistEntry.race_id == race.id, WaitlistEntry.email == email)
    ).first()
    if already:
        raise MarketError("Cette adresse est déjà inscrite sur la liste d'attente de cette course.")
    entry = WaitlistEntry(race_id=race.id, first_name=first_name, last_name=last_name, email=email)
    session.add(entry)
    notify(
        session,
        email,
        f"Vous êtes sur la liste d'attente — {race.name}",
        "Nous vous écrirons dès qu'un dossard vous sera proposé. Vous aurez alors 48 heures pour le payer.",
        link=f"/runner/races/{race.id}",
    )
    session.commit()
    offer_pending(session, race.id)
    return entry


def position(session: Session, entry: WaitlistEntry) -> int | None:
    """Le rang du coureur dans la file (1 = le prochain servi), ou None s'il a déjà une offre."""
    waiting = [candidate.id for candidate in _waiting(session, entry.race_id)]
    return waiting.index(entry.id) + 1 if entry.id in waiting else None


def current_offer(session: Session, entry: WaitlistEntry) -> Transfer | None:
    """L'offre en attente de paiement faite à ce coureur, s'il y en a une."""
    statement = select(Transfer).where(Transfer.buyer_id == entry.id, Transfer.state == TransferState.RESERVE)
    return session.exec(statement).first()


def create_listing(
    session: Session,
    race: Race,
    *,
    bib_number: int,
    first_name: str,
    last_name: str,
    seller_email: str,
    price_cents: int,
    today: date | None = None,
) -> Transfer:
    """Met un dossard en vente, après contrôle du règlement et du registre, puis le propose au suivant."""
    rules = rules_for(session, race.id)
    check_resale_open(race, rules, today or date.today())
    if price_cents <= 0:
        raise MarketError("Le prix doit être supérieur à zéro.")
    cap_cents = rules.max_resale_price_cents if rules else None
    if cap_cents is not None and price_cents > cap_cents:
        raise MarketError(f"Le règlement de cette course plafonne la revente à {euros(cap_cents)} €.")

    # Seul le titulaire du dossard peut le vendre : son nom doit être celui du registre de l'organisateur.
    registration = get_registration(session, race.id, bib_number)
    if registration is None:
        raise MarketError(f"Aucun dossard n°{bib_number} parmi les inscrits de cette course.")
    holder = f"{registration.first_name} {registration.last_name}"
    if compare_names(holder, f"{first_name} {last_name}") is not NameMatch.EXACT:
        raise MarketError(f"Le dossard n°{bib_number} n'est pas inscrit à ce nom.")

    in_progress = (
        select(Transfer)
        .join(Listing, Listing.id == Transfer.listing_id)
        .where(Listing.race_id == race.id, Listing.bib_number == bib_number, col(Transfer.state).not_in(FINISHED))
    )
    if session.exec(in_progress).first():
        raise MarketError(f"Le dossard n°{bib_number} est déjà en vente.")

    listing = Listing(race_id=race.id, bib_number=bib_number, seller_email=seller_email, price_cents=price_cents)
    session.add(listing)
    session.flush()  # attribue listing.id
    transfer = Transfer(listing_id=listing.id)
    session.add(transfer)
    notify(
        session,
        seller_email,
        f"Votre dossard n°{bib_number} est en vente — {race.name}",
        f"Prix demandé : {euros(price_cents)} €. Il est proposé aux coureurs de la liste d'attente, dans l'ordre "
        "de leur inscription. Vous serez payé quand l'organisateur aura mis le dossard au nom de l'acheteur.",
        link=f"/runner/listings/{listing.id}",
    )
    session.commit()
    offer_next(session, transfer)
    return transfer


def expire_offers(session: Session, now: datetime | None = None) -> int:
    """Clôt les offres dont le délai de paiement est dépassé et propose chaque dossard au suivant."""
    now = now or utcnow()
    due = session.exec(
        select(Transfer).where(Transfer.state == TransferState.RESERVE, col(Transfer.offer_expires_at) < now)
    ).all()
    for transfer in due:
        listing = session.get(Listing, transfer.listing_id)
        race = session.get(Race, listing.race_id)
        buyer = session.get(WaitlistEntry, transfer.buyer_id)
        transfer.state = next_state(transfer.state, TransferEvent.EXPIRER)
        notify(
            session,
            buyer.email,
            f"Votre offre a expiré — {race.name}",
            f"Le délai pour payer le dossard n°{listing.bib_number} est dépassé : il est proposé au coureur suivant. "
            "Vous restez sur la liste d'attente.",
            link=f"/runner/races/{race.id}",
        )
        # Une nouvelle tentative pour le même dossard : les identifiants PayPal ne passent pas d'un acheteur à l'autre.
        fresh = Transfer(listing_id=listing.id)
        session.add_all([transfer, fresh])
        session.commit()
        offer_next(session, fresh, now)
    return len(due)
