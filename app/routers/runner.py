"""Pages du coureur : la course, la liste d'attente, la mise en vente, l'achat et le suivi."""

import logging
import tempfile
from datetime import date, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from app.ai.client import AIError
from app.config import settings
from app.db import SessionDep
from app.forms import parse_bib, parse_email, parse_euros, parse_name
from app.marketplace import service as market
from app.marketplace.service import MarketError
from app.models import Listing, Race, Transfer, Verification, WaitlistEntry, utcnow
from app.notifications import notify
from app.payments import service
from app.payments.client import PayPalError
from app.payments.commission import compute_amounts
from app.templating import euros, templates
from app.transfers.state import InvalidTransition, TransferEvent, TransferState, next_state
from app.transfers.verification import MAX_PROOF_BYTES, submit_proof

router = APIRouter(prefix="/runner", tags=["runner"])
logger = logging.getLogger(__name__)

# Ce qu'on dit à l'acheteur quand PayPal refuse : le détail technique part dans les logs du serveur.
PAYPAL_REFUSAL = "PayPal n'a pas accepté l'opération. Rien n'a été débité : vous pouvez réessayer."


def get_offer(session: Session, transfer_id: int) -> Transfer:
    """Le transfert, s'il a bien été proposé à un acheteur."""
    transfer = session.get(Transfer, transfer_id)
    if transfer is None or transfer.buyer_id is None:
        raise HTTPException(status_code=404, detail="Offre inconnue")
    return transfer


def offer_status(transfer: Transfer) -> str:
    """Où en est l'offre, vue par l'acheteur : payable, expired, authorized, paid ou refused."""
    if transfer.state is TransferState.RESERVE:
        expires_at = transfer.offer_expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            # Selon la base, la date peut revenir sans fuseau : elle est toujours en UTC.
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return "expired" if expires_at is not None and utcnow() > expires_at else "payable"
    if transfer.state is TransferState.OFFRE_EXPIREE:
        return "expired"
    if transfer.state is TransferState.REFUSE:
        return "refused"
    if transfer.state in (TransferState.ENCAISSE, TransferState.VERSE):
        return "paid"
    return "authorized"  # AUTORISE, VERIFIE, NOM_CHANGE : fonds bloqués, pas encore débités


@router.get("/transfers/{transfer_id}/checkout", response_class=HTMLResponse)
def checkout_page(transfer_id: int, request: Request, session: SessionDep):
    transfer = get_offer(session, transfer_id)
    listing = session.get(Listing, transfer.listing_id)
    context = {
        "transfer": transfer,
        "listing": listing,
        "race": session.get(Race, listing.race_id),
        "buyer": session.get(WaitlistEntry, transfer.buyer_id),
        "amounts": service.amounts_for(session, transfer),
        "status": offer_status(transfer),
        # L'identifiant client est public : c'est le secret qui ne doit jamais quitter le serveur.
        "paypal_client_id": settings.paypal_client_id,
    }
    return templates.TemplateResponse(request, "runner/checkout.html", context)


@router.post("/transfers/{transfer_id}/order")
def create_order(transfer_id: int, session: SessionDep):
    """Appelé au clic sur le bouton PayPal. Le montant est calculé ici, jamais par le navigateur."""
    transfer = get_offer(session, transfer_id)
    if offer_status(transfer) != "payable":
        raise HTTPException(status_code=409, detail="Cette offre ne peut plus être payée.")
    try:
        order = service.create_order(session, transfer)
    except PayPalError as error:
        logger.error("Création de commande refusée pour le transfert %s : %s", transfer_id, error)
        raise HTTPException(status_code=502, detail=PAYPAL_REFUSAL) from error
    return {"id": order.id}


@router.post("/transfers/{transfer_id}/authorize")
def authorize(transfer_id: int, session: SessionDep):
    """Appelé quand l'acheteur a approuvé chez PayPal : bloque les fonds, sans débiter."""
    transfer = get_offer(session, transfer_id)
    if transfer.state is TransferState.AUTORISE:
        # Déjà fait (double clic, nouvel essai après une coupure) : rien à refaire.
        return {"state": transfer.state}
    if offer_status(transfer) != "payable":
        raise HTTPException(status_code=409, detail="Cette offre ne peut plus être payée.")
    try:
        # On autorise la commande enregistrée sur le transfert, pas un identifiant venu du navigateur.
        service.authorize(session, transfer)
    except service.PaymentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PayPalError as error:
        logger.error("Autorisation refusée pour le transfert %s : %s", transfer_id, error)
        raise HTTPException(status_code=502, detail=PAYPAL_REFUSAL) from error
    transfer.state = next_state(transfer.state, TransferEvent.AUTORISER)
    session.add(transfer)
    listing = session.get(Listing, transfer.listing_id)
    race = session.get(Race, listing.race_id)
    notify(
        session,
        session.get(WaitlistEntry, transfer.buyer_id).email,
        f"Paiement autorisé : déposez votre justificatif — {race.name}",
        "La somme est bloquée, vous n'êtes pas encore débité. Déposez votre licence FFA ou votre PPS pour "
        "que le dossard soit mis à votre nom.",
        link=f"/runner/transfers/{transfer.id}",
    )
    notify(
        session,
        listing.seller_email,
        f"Un acheteur a payé votre dossard n°{listing.bib_number}",
        "La somme est bloquée chez PayPal. Vous serez payé dès que l'acheteur sera vérifié et que le dossard "
        "sera à son nom.",
        link=f"/runner/listings/{listing.id}",
    )
    session.commit()
    return {"state": transfer.state}


# --- La course, la liste d'attente, la mise en vente ---


def get_race(session: Session, race_id: int) -> Race:
    race = session.get(Race, race_id)
    if race is None:
        raise HTTPException(status_code=404, detail="Course inconnue")
    return race


def race_context(session: Session, race: Race) -> dict:
    rules = market.rules_for(session, race.id)
    try:
        market.check_resale_open(race, rules, date.today())
        closed = None
    except MarketError as error:
        closed = str(error)
    return {"race": race, "rules": rules, "market": market.race_market(session, race.id), "closed": closed}


def message(request: Request, text: str, *, error: bool = False) -> HTMLResponse:
    """Un court message, que HTMX insère dans la page. Une erreur part en 422."""
    context = {"text": text, "error": error}
    return templates.TemplateResponse(request, "runner/_message.html", context, status_code=422 if error else 200)


@router.get("/races/{race_id}", response_class=HTMLResponse)
def race_page(race_id: int, request: Request, session: SessionDep):
    return templates.TemplateResponse(request, "runner/race.html", race_context(session, get_race(session, race_id)))


@router.post("/races/{race_id}/waitlist", response_class=HTMLResponse)
def join_waitlist(
    race_id: int,
    request: Request,
    session: SessionDep,
    first_name: Annotated[str, Form()] = "",
    last_name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
):
    race = get_race(session, race_id)
    try:
        entry = market.join_waitlist(
            session, race, parse_name(first_name, "Prénom"), parse_name(last_name, "Nom"), parse_email(email)
        )
    except (ValueError, MarketError) as error:
        return message(request, str(error), error=True)
    context = {"entry": entry, "position": market.position(session, entry), "offer": market.current_offer(session, entry)}
    return templates.TemplateResponse(request, "runner/_waitlist_joined.html", context)


@router.get("/races/{race_id}/sell", response_class=HTMLResponse)
def sell_page(race_id: int, request: Request, session: SessionDep):
    return templates.TemplateResponse(request, "runner/sell.html", race_context(session, get_race(session, race_id)))


@router.get("/races/{race_id}/price-check", response_class=HTMLResponse)
def price_check(race_id: int, request: Request, session: SessionDep, price: str = ""):
    """Appelé par HTMX pendant la saisie du prix : respecte-t-il le plafond, et que paiera l'acheteur ?"""
    race = get_race(session, race_id)
    rules = market.rules_for(session, race_id)
    try:
        price_cents = parse_euros(price)
    except ValueError as error:
        return message(request, str(error), error=True)
    if price_cents is None:
        return HTMLResponse("")
    cap_cents = rules.max_resale_price_cents if rules else None
    if price_cents == 0:
        return message(request, "Le prix doit être supérieur à zéro.", error=True)
    if cap_cents is not None and price_cents > cap_cents:
        return message(request, f"Le règlement de cette course plafonne la revente à {euros(cap_cents)} €.", error=True)
    amounts = compute_amounts(price_cents, race.platform_fee_bps, race.organizer_fee_bps)
    return message(
        request,
        f"Prix accepté. Vous recevrez {euros(amounts.price_cents)} € ; l'acheteur paiera "
        f"{euros(amounts.total_cents)} €, frais de service de {euros(amounts.service_fee_cents)} € compris.",
    )


@router.post("/races/{race_id}/listings")
def create_listing(
    race_id: int,
    request: Request,
    session: SessionDep,
    bib_number: Annotated[str, Form()] = "",
    first_name: Annotated[str, Form()] = "",
    last_name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    price: Annotated[str, Form()] = "",
):
    race = get_race(session, race_id)
    try:
        price_cents = parse_euros(price)
        if price_cents is None:
            raise ValueError("Indiquez le prix demandé.")
        transfer = market.create_listing(
            session,
            race,
            bib_number=parse_bib(bib_number),
            first_name=parse_name(first_name, "Prénom"),
            last_name=parse_name(last_name, "Nom"),
            seller_email=parse_email(email),
            price_cents=price_cents,
        )
    except (ValueError, MarketError) as error:
        return message(request, str(error), error=True)
    # HTMX lit cet en-tête et ouvre la page de suivi de l'annonce.
    return Response(status_code=204, headers={"HX-Redirect": f"/runner/listings/{transfer.listing_id}"})


# --- Le suivi et le justificatif ---

# Les étapes d'un transfert qui aboutit, dans l'ordre, telles que la page de suivi les affiche.
STEPS = [
    (TransferState.EN_VENTE, "En vente"),
    (TransferState.RESERVE, "Proposé à un coureur"),
    (TransferState.AUTORISE, "Paiement autorisé"),
    (TransferState.VERIFIE, "Justificatif vérifié"),
    (TransferState.NOM_CHANGE, "Dossard au nom de l'acheteur"),
    (TransferState.ENCAISSE, "Paiement encaissé"),
    (TransferState.VERSE, "Vendeur payé"),
]


def seen_token(transfer: Transfer) -> str:
    """Ce que la page de suivi a affiché : si ça ne correspond plus à la base, elle doit se recharger."""
    return f"{transfer.id}-{transfer.state}"


def refresh_if_changed(transfer: Transfer, seen: str) -> Response:
    """Réponse à l'interrogation périodique de la page de suivi : la recharger si l'état a changé."""
    headers = {} if seen == seen_token(transfer) else {"HX-Refresh": "true"}
    return Response(status_code=204, headers=headers)


def tracking_page(request: Request, session: Session, transfer: Transfer, viewer: str, status_url: str):
    listing = session.get(Listing, transfer.listing_id)
    states = [state for state, _ in STEPS]
    last_verification = session.exec(
        select(Verification).where(Verification.transfer_id == transfer.id).order_by(Verification.id.desc())
    ).first()
    # Un vendeur continue de suivre son dossard après une offre expirée : il sera proposé au suivant.
    finished = (TransferState.VERSE, TransferState.REFUSE) if viewer == "seller" else market.FINISHED
    context = {
        "transfer": transfer,
        "listing": listing,
        "race": session.get(Race, listing.race_id),
        "buyer": session.get(WaitlistEntry, transfer.buyer_id) if transfer.buyer_id else None,
        "viewer": viewer,
        "steps": STEPS,
        "reached": states.index(transfer.state) if transfer.state in states else None,
        "status": offer_status(transfer),
        "verification": last_verification,
        "poll_url": None if transfer.state in finished else f"{status_url}?seen={seen_token(transfer)}",
    }
    return templates.TemplateResponse(request, "runner/tracking.html", context)


def latest_transfer(session: Session, listing_id: int) -> Transfer:
    """La tentative en cours pour cette annonce : la dernière créée."""
    statement = select(Transfer).where(Transfer.listing_id == listing_id).order_by(Transfer.id.desc())
    transfer = session.exec(statement).first()
    if transfer is None:
        raise HTTPException(status_code=404, detail="Annonce inconnue")
    return transfer


@router.get("/transfers/{transfer_id}", response_class=HTMLResponse)
def buyer_tracking(transfer_id: int, request: Request, session: SessionDep):
    transfer = get_offer(session, transfer_id)
    return tracking_page(request, session, transfer, "buyer", f"/runner/transfers/{transfer_id}/status")


@router.get("/transfers/{transfer_id}/status")
def buyer_tracking_status(transfer_id: int, session: SessionDep, seen: str = ""):
    return refresh_if_changed(get_offer(session, transfer_id), seen)


@router.get("/listings/{listing_id}", response_class=HTMLResponse)
def seller_tracking(listing_id: int, request: Request, session: SessionDep):
    transfer = latest_transfer(session, listing_id)
    return tracking_page(request, session, transfer, "seller", f"/runner/listings/{listing_id}/status")


@router.get("/listings/{listing_id}/status")
def seller_tracking_status(listing_id: int, session: SessionDep, seen: str = ""):
    return refresh_if_changed(latest_transfer(session, listing_id), seen)


@router.post("/transfers/{transfer_id}/proof")
def upload_proof(transfer_id: int, request: Request, session: SessionDep, justificatif: UploadFile):
    transfer = get_offer(session, transfer_id)
    # Le justificatif n'existe sur le disque que le temps de l'analyse : submit_proof le supprime, quoi qu'il arrive.
    with tempfile.NamedTemporaryFile(delete=False, prefix="relais-justificatif-") as file:
        file.write(justificatif.file.read(MAX_PROOF_BYTES + 1))
    try:
        submit_proof(session, transfer, Path(file.name))
    except AIError as error:
        return message(request, str(error), error=True)
    except InvalidTransition:
        return message(request, "Ce transfert n'attend pas de justificatif.", error=True)
    # La page se recharge et affiche le verdict.
    return Response(status_code=204, headers={"HX-Refresh": "true"})
