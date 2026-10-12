"""Marketplace : mise en vente d'un dossard vérifié, réservation par un acheteur, contrôles avant paiement.

Un dossard n'est en vente qu'après vérification auprès de l'organisateur. Un acheteur qui clique
« Acheter ce dossard » le réserve quelques minutes ; ses contrôles passent avant tout paiement.
Une réservation qui s'arrête (délai dépassé, acheteur inéligible) remet le dossard en vente.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import update
from sqlmodel import Session, col, select

from app.ai.verifier import NameMatch, compare_names
from app.config import settings
from app.models import Buyer, Listing, Race, RaceRulesRow, Transfer, WaitlistEntry, utcnow
from app.notifications import notify
from app.registry_mock.registry import get_registration, list_registrations
from app.templating import euros
from app.transfers.state import FINAL_STATES, TransferEvent, TransferState, next_state

# Tant que l'acheteur n'a pas payé, le dossard lui est seulement réservé.
HELD = (TransferState.RESERVE, TransferState.ELIGIBLE)


class MarketError(Exception):
    """Demande refusée. Le message est en français et peut être montré tel quel au coureur."""


class Ineligible(MarketError):
    """L'acheteur n'a pas passé les contrôles. Le dossard est déjà reparti en vente, rien n'a été payé."""


@dataclass(frozen=True)
class RaceMarket:
    """Où en est la bourse d'une course."""

    free: int  # dossards en vente, que l'on peut acheter
    reserved: int  # dossards réservés par un acheteur qui n'a pas encore payé
    waiting: int  # coureurs qui ont demandé à être prévenus


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


def bibs_on_sale(session: Session, race_id: int) -> list[tuple[Listing, Transfer]]:
    """Les dossards visibles sur la page d'une course, avec leur transfert en cours : les libres d'abord."""
    statement = (
        select(Listing, Transfer)
        .join(Transfer, Transfer.listing_id == Listing.id)
        .where(Listing.race_id == race_id, col(Transfer.state).in_((TransferState.EN_VENTE, *HELD)))
    )
    return sorted(session.exec(statement).all(), key=lambda pair: (pair[1].state in HELD, pair[0].bib_number))


def race_market(session: Session, race_id: int) -> RaceMarket:
    states = [transfer.state for _, transfer in bibs_on_sale(session, race_id)]
    waiting = session.exec(select(WaitlistEntry.id).where(WaitlistEntry.race_id == race_id)).all()
    return RaceMarket(
        free=states.count(TransferState.EN_VENTE),
        reserved=sum(1 for state in states if state in HELD),
        waiting=len(waiting),
    )


# --- Mise en vente ---


def put_on_sale(session: Session, listing: Listing) -> Transfer:
    """Ouvre un nouveau transfert pour ce dossard, déjà vérifié, et le rend visible des acheteurs."""
    transfer = Transfer(listing_id=listing.id)  # naît à l'état DOSSARD_VERIFIE
    transfer.state = next_state(transfer.state, TransferEvent.METTRE_EN_VENTE)
    session.add(transfer)
    return transfer


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
    """Vérifie le dossard auprès de l'organisateur, puis le met en vente. Sans vérification, rien n'est créé."""
    rules = rules_for(session, race.id)
    check_resale_open(race, rules, today or date.today())
    if price_cents <= 0:
        raise MarketError("Le prix doit être supérieur à zéro.")
    cap_cents = rules.max_resale_price_cents if rules else None
    if cap_cents is not None and price_cents > cap_cents:
        raise MarketError(f"Le règlement de cette course plafonne la revente à {euros(cap_cents)} €.")

    # La vérification du dossard : il doit exister dans le registre de l'organisateur, au nom du vendeur.
    # Le registre est simulé pour l'instant (app/registry_mock).
    registration = get_registration(session, race.id, bib_number)
    if registration is None:
        raise MarketError(f"Aucun dossard n°{bib_number} parmi les inscrits de « {race.name} ».")
    holder = f"{registration.first_name} {registration.last_name}"
    if compare_names(holder, f"{first_name} {last_name}") is not NameMatch.EXACT:
        raise MarketError(f"Le dossard n°{bib_number} n'est pas inscrit à ce nom.")

    in_progress = (
        select(Transfer)
        .join(Listing, Listing.id == Transfer.listing_id)
        .where(Listing.race_id == race.id, Listing.bib_number == bib_number, col(Transfer.state).not_in(FINAL_STATES))
    )
    if session.exec(in_progress).first():
        raise MarketError(f"Le dossard n°{bib_number} est déjà en vente.")

    listing = Listing(race_id=race.id, bib_number=bib_number, seller_email=seller_email, price_cents=price_cents)
    session.add(listing)
    session.flush()  # attribue listing.id
    transfer = put_on_sale(session, listing)
    notify(
        session,
        seller_email,
        f"Votre dossard n°{bib_number} est vérifié et en vente — {race.name}",
        f"Prix demandé : {euros(price_cents)} €. Vous serez payé quand l'organisateur aura mis le dossard "
        "au nom de l'acheteur.",
        link=f"/runner/listings/{listing.id}",
    )
    for entry in session.exec(select(WaitlistEntry).where(WaitlistEntry.race_id == race.id)).all():
        notify(
            session,
            entry.email,
            f"Un dossard est en vente — {race.name}",
            f"Le dossard n°{bib_number} vient d'être mis en vente à {euros(price_cents)} €. Le premier qui "
            "l'achète l'emporte.",
            link=f"/runner/races/{race.id}",
        )
    session.commit()
    return transfer


# --- Réservation et contrôles de l'acheteur ---


def reserve(session: Session, transfer_id: int, now: datetime | None = None) -> Transfer:
    """Un acheteur a cliqué « Acheter ce dossard » : le dossard lui est réservé le temps de payer."""
    now = now or utcnow()
    target = next_state(TransferState.EN_VENTE, TransferEvent.RESERVER)
    # Une seule requête qui teste et modifie : si deux acheteurs cliquent en même temps, un seul l'emporte.
    result = session.exec(
        update(Transfer)
        .where(col(Transfer.id) == transfer_id, col(Transfer.state) == TransferState.EN_VENTE)
        .values(state=target, offer_expires_at=now + timedelta(minutes=settings.reservation_minutes))
    )
    session.commit()
    if result.rowcount != 1:
        raise MarketError("Ce dossard vient d'être réservé par un autre coureur.")
    transfer = session.get(Transfer, transfer_id)
    session.refresh(transfer)
    return transfer


def reservation_expired(transfer: Transfer, now: datetime | None = None) -> bool:
    return transfer.offer_expires_at is not None and (now or utcnow()) > transfer.offer_expires_at


def age_on(day: date, birth_date: date) -> int:
    return day.year - birth_date.year - ((day.month, day.day) < (birth_date.month, birth_date.day))


def eligibility_problem(session: Session, race: Race, first_name: str, last_name: str, birth_date: date) -> str | None:
    """Ce qui empêche cette personne de reprendre un dossard de cette course, ou None si rien ne s'y oppose."""
    if age_on(race.event_date, birth_date) < race.minimum_age:
        return f"Il faut avoir {race.minimum_age} ans le jour de la course pour y participer."
    name = f"{first_name} {last_name}"
    for registration in list_registrations(session, race.id):
        if compare_names(f"{registration.first_name} {registration.last_name}", name) is NameMatch.EXACT:
            return "Vous êtes déjà inscrit à cette course : un coureur ne peut pas y avoir deux dossards."
    return None


def register_buyer(
    session: Session,
    transfer: Transfer,
    *,
    first_name: str,
    last_name: str,
    email: str,
    birth_date: date,
    now: datetime | None = None,
) -> Buyer:
    """Enregistre l'identité de l'acheteur et fait les contrôles, avant tout paiement.

    S'il est éligible, le transfert passe à ELIGIBLE : il peut payer. Sinon le transfert s'arrête,
    le dossard repart en vente, et Ineligible porte la raison à lui montrer.
    """
    if transfer.state is not TransferState.RESERVE or reservation_expired(transfer, now):
        raise MarketError("Cette réservation n'est plus valable. Retournez sur la page de la course.")
    listing = session.get(Listing, transfer.listing_id)
    race = session.get(Race, listing.race_id)
    buyer = Buyer(race_id=race.id, first_name=first_name, last_name=last_name, email=email, birth_date=birth_date)
    session.add(buyer)
    session.flush()  # attribue buyer.id
    transfer.buyer_id = buyer.id

    problem = eligibility_problem(session, race, first_name, last_name, birth_date)
    if problem:
        transfer.state = next_state(transfer.state, TransferEvent.REFUSER_ACHETEUR)
        session.add(transfer)
        put_on_sale(session, listing)
        session.commit()
        raise Ineligible(problem)
    transfer.state = next_state(transfer.state, TransferEvent.VALIDER_ACHETEUR)
    session.add(transfer)
    session.commit()
    return buyer


def expire_reservations(session: Session, now: datetime | None = None) -> int:
    """Clôt les réservations dont le délai est passé sans paiement et remet chaque dossard en vente."""
    now = now or utcnow()
    due = session.exec(
        select(Transfer).where(col(Transfer.state).in_(HELD), col(Transfer.offer_expires_at) < now)
    ).all()
    for transfer in due:
        listing = session.get(Listing, transfer.listing_id)
        race = session.get(Race, listing.race_id)
        transfer.state = next_state(transfer.state, TransferEvent.EXPIRER)
        session.add(transfer)
        if transfer.buyer_id:
            notify(
                session,
                session.get(Buyer, transfer.buyer_id).email,
                f"Votre réservation a expiré — {race.name}",
                f"Le délai pour payer le dossard n°{listing.bib_number} est dépassé : il est de nouveau en vente. "
                "Rien n'a été débité.",
                link=f"/runner/races/{race.id}",
            )
        # Un nouveau transfert pour le même dossard : les identifiants PayPal ne passent pas d'un acheteur à l'autre.
        put_on_sale(session, listing)
        session.commit()
    return len(due)


# --- Liste d'attente : être prévenu quand un dossard est mis en vente ---


def join_waitlist(
    session: Session, race: Race, first_name: str, last_name: str, email: str, today: date | None = None
) -> WaitlistEntry:
    """Inscrit un coureur sur la liste d'attente : il sera prévenu de chaque dossard mis en vente."""
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
        "Nous vous écrirons dès qu'un dossard sera mis en vente. Le premier qui l'achète l'emporte.",
        link=f"/runner/races/{race.id}",
    )
    session.commit()
    return entry
