"""Pages du coureur : la course, la mise en vente, l'achat (réservation, contrôles, paiement) et le suivi."""

import logging
import tempfile
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from app.ai.client import AIError
from app.config import settings
from app.db import SessionDep
from app.forms import parse_bib, parse_birth_date, parse_email, parse_euros, parse_name
from app.marketplace import service as market
from app.marketplace.service import Ineligible, MarketError
from app.models import Buyer, Listing, Race, Transfer
from app.notifications import notify
from app.payments import service
from app.payments.client import PayPalError
from app.payments.commission import compute_amounts
from app.registry_mock.registry import list_registrations
from app.templating import euros, templates
from app.transfers.state import FINAL_STATES, TransferEvent, TransferState, next_state
from app.transfers.verification import (
    MAX_PROOF_BYTES,
    PURCHASED,
    ProofNotExpected,
    ProofStatus,
    last_verification,
    proof_status,
    submit_proof,
)

router = APIRouter(prefix="/runner", tags=["runner"])
logger = logging.getLogger(__name__)

# Ce qu'on dit à l'acheteur quand PayPal refuse : le détail technique part dans les logs du serveur.
PAYPAL_REFUSAL = "PayPal n'a pas accepté l'opération. Rien n'a été débité : vous pouvez réessayer."

# À partir de ces états, le dossard est au nom de son nouveau coureur.
TRANSFERRED = (TransferState.NOM_CHANGE, TransferState.ENCAISSE, TransferState.VERSE)

# Les étapes d'un transfert qui aboutit, dans l'ordre, telles que la page de suivi les affiche.
STEPS = [
    (TransferState.DOSSARD_VERIFIE, "Dossard vérifié"),
    (TransferState.EN_VENTE, "En vente"),
    (TransferState.RESERVE, "Réservé"),
    (TransferState.ELIGIBLE, "Acheteur éligible"),
    (TransferState.AUTORISE, "Paiement autorisé"),
    (TransferState.NOM_CHANGE, "Dossard au nom de l'acheteur"),
    (TransferState.ENCAISSE, "Paiement encaissé"),
    (TransferState.VERSE, "Vendeur payé, commission versée"),
]


def get_race(session: Session, race_id: int) -> Race:
    race = session.get(Race, race_id)
    if race is None:
        raise HTTPException(status_code=404, detail="Course inconnue")
    return race


def get_transfer(session: Session, transfer_id: int) -> Transfer:
    transfer = session.get(Transfer, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Dossard inconnu")
    return transfer


def get_offer(session: Session, transfer_id: int) -> Transfer:
    """Le transfert, une fois que son acheteur a donné son identité."""
    transfer = session.get(Transfer, transfer_id)
    if transfer is None or transfer.buyer_id is None:
        raise HTTPException(status_code=404, detail="Achat inconnu")
    return transfer


def offer_status(transfer: Transfer) -> str:
    """Où en est l'achat, vu par l'acheteur : payable, expired, ineligible, authorized, paid ou refused."""
    if transfer.state is TransferState.ELIGIBLE:
        return "expired" if market.reservation_expired(transfer) else "payable"
    if transfer.state is TransferState.RESERVATION_EXPIREE:
        return "expired"
    if transfer.state is TransferState.INELIGIBLE:
        return "ineligible"
    if transfer.state is TransferState.REFUSE:
        return "refused"
    if transfer.state in (TransferState.ENCAISSE, TransferState.VERSE):
        return "paid"
    return "authorized"  # AUTORISE, NOM_CHANGE : fonds bloqués, pas encore débités


def message(request: Request, text: str, *, error: bool = False) -> HTMLResponse:
    """Un court message, que HTMX insère dans la page. Une erreur part en 422."""
    context = {"text": text, "error": error}
    return templates.TemplateResponse(request, "runner/_message.html", context, status_code=422 if error else 200)


def stopped(request: Request, session: Session, transfer: Transfer, title: str, text: str) -> HTMLResponse:
    """La page affichée quand un achat s'arrête sans paiement : la raison, et le chemin pour repartir."""
    listing = session.get(Listing, transfer.listing_id)
    context = {"listing": listing, "race": session.get(Race, listing.race_id), "title": title, "text": text}
    return templates.TemplateResponse(request, "runner/stopped.html", context)


# --- La course, la liste d'attente, la mise en vente ---


def race_context(session: Session, race: Race) -> dict:
    rules = market.rules_for(session, race.id)
    try:
        market.check_resale_open(race, rules, date.today())
        closed = None
    except MarketError as error:
        closed = str(error)
    return {
        "race": race,
        "rules": rules,
        "market": market.race_market(session, race.id),
        "bibs": market.bibs_on_sale(session, race.id),
        "closed": closed,
        "reservation_minutes": settings.reservation_minutes,
    }


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
    return templates.TemplateResponse(request, "runner/_waitlist_joined.html", {"entry": entry})


@router.get("/races/{race_id}/sell", response_class=HTMLResponse)
def sell_page(race_id: int, request: Request, session: SessionDep):
    race = get_race(session, race_id)
    # Le registre est simulé : on montre ses inscrits, pour qu'un visiteur de la démo sache quel dossard vendre.
    context = {**race_context(session, race), "registrations": list_registrations(session, race.id)}
    return templates.TemplateResponse(request, "runner/sell.html", context)


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


# --- L'achat : réserver, donner son identité, payer ---


@router.post("/transfers/{transfer_id}/reserve")
def reserve_bib(transfer_id: int, request: Request, session: SessionDep):
    """Le clic sur « Acheter ce dossard » : le dossard est réservé, puis on demande son identité à l'acheteur."""
    transfer = get_transfer(session, transfer_id)
    race = session.get(Race, session.get(Listing, transfer.listing_id).race_id)
    try:
        market.check_resale_open(race, market.rules_for(session, race.id), date.today())
        market.reserve(session, transfer_id)
    except MarketError as error:
        context = {**race_context(session, race), "error": str(error)}
        return templates.TemplateResponse(request, "runner/race.html", context, status_code=409)
    return RedirectResponse(f"/runner/transfers/{transfer_id}/buyer", status_code=303)


def buyer_form(request: Request, session: Session, transfer: Transfer, values: dict | None = None, error: str = ""):
    listing = session.get(Listing, transfer.listing_id)
    race = session.get(Race, listing.race_id)
    context = {
        "transfer": transfer,
        "listing": listing,
        "race": race,
        "amounts": service.amounts_for(session, transfer),
        "values": values or {},
        "error": error,
    }
    return templates.TemplateResponse(request, "runner/buyer.html", context, status_code=422 if error else 200)


@router.get("/transfers/{transfer_id}/buyer", response_class=HTMLResponse)
def buyer_page(transfer_id: int, request: Request, session: SessionDep):
    transfer = get_transfer(session, transfer_id)
    if transfer.buyer_id is not None:
        return RedirectResponse(f"/runner/transfers/{transfer_id}", status_code=303)
    if transfer.state is not TransferState.RESERVE or market.reservation_expired(transfer):
        text = "Le délai de réservation est passé : le dossard est de nouveau en vente. Rien n'a été débité."
        return stopped(request, session, transfer, "Cette réservation a expiré", text)
    return buyer_form(request, session, transfer)


@router.post("/transfers/{transfer_id}/buyer")
def register_buyer(
    transfer_id: int,
    request: Request,
    session: SessionDep,
    first_name: Annotated[str, Form()] = "",
    last_name: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    birth_date: Annotated[str, Form()] = "",
):
    """Les contrôles de l'acheteur, avant tout paiement. S'ils passent, il est envoyé vers le paiement."""
    transfer = get_transfer(session, transfer_id)
    values = {"first_name": first_name, "last_name": last_name, "email": email, "birth_date": birth_date}
    try:
        identity = {
            "first_name": parse_name(first_name, "Prénom"),
            "last_name": parse_name(last_name, "Nom"),
            "email": parse_email(email),
            "birth_date": parse_birth_date(birth_date),
        }
    except ValueError as error:
        # Une saisie à corriger : la réservation tient toujours.
        return buyer_form(request, session, transfer, values, str(error))
    try:
        market.register_buyer(session, transfer, **identity)
    except Ineligible as error:
        text = f"{error} Le dossard est de nouveau en vente et aucun paiement n'a été déclenché."
        return stopped(request, session, transfer, "Vous ne pouvez pas reprendre ce dossard", text)
    except MarketError as error:
        return stopped(request, session, transfer, "Cette réservation a expiré", str(error))
    return RedirectResponse(f"/runner/transfers/{transfer_id}/checkout", status_code=303)


@router.get("/transfers/{transfer_id}/checkout", response_class=HTMLResponse)
def checkout_page(transfer_id: int, request: Request, session: SessionDep):
    transfer = get_offer(session, transfer_id)
    listing = session.get(Listing, transfer.listing_id)
    context = {
        "transfer": transfer,
        "listing": listing,
        "race": session.get(Race, listing.race_id),
        "buyer": session.get(Buyer, transfer.buyer_id),
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
        raise HTTPException(status_code=409, detail="Ce dossard ne peut plus être payé.")
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
    if transfer.state in PURCHASED:
        # Déjà fait (double clic, nouvel essai après une coupure) : rien à refaire.
        return {"state": transfer.state}
    if offer_status(transfer) != "payable":
        raise HTTPException(status_code=409, detail="Ce dossard ne peut plus être payé.")
    try:
        # On autorise la commande enregistrée sur le transfert, pas un identifiant venu du navigateur.
        service.authorize(session, transfer)
    except service.PaymentError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PayPalError as error:
        logger.error("Autorisation refusée pour le transfert %s : %s", transfer_id, error)
        raise HTTPException(status_code=502, detail=PAYPAL_REFUSAL) from error
    # Seul un acheteur éligible peut arriver ici : la machine à états refuse tout autre chemin.
    transfer.state = next_state(transfer.state, TransferEvent.AUTORISER)
    session.add(transfer)
    listing = session.get(Listing, transfer.listing_id)
    race = session.get(Race, listing.race_id)
    notify(
        session,
        session.get(Buyer, transfer.buyer_id).email,
        f"Paiement autorisé — {race.name}",
        "La somme est bloquée, vous n'êtes pas encore débité. Vous le serez quand l'organisateur aura mis "
        "le dossard à votre nom.",
        link=f"/runner/transfers/{transfer.id}",
    )
    notify(
        session,
        listing.seller_email,
        f"Un acheteur a payé votre dossard n°{listing.bib_number}",
        "La somme est bloquée chez PayPal. Vous serez payé dès que l'organisateur aura mis le dossard au nom "
        "de l'acheteur.",
        link=f"/runner/listings/{listing.id}",
    )
    session.commit()
    return {"state": transfer.state}


# --- Le suivi et le justificatif ---


def bib_band(transfer: Transfer, viewer: str, buyer: Buyer | None) -> str:
    """Ce qu'on lit sur le bandeau du dossard. Le nom de l'acheteur n'est montré qu'à l'acheteur."""
    if transfer.state in TRANSFERRED:
        return f"{buyer.first_name} {buyer.last_name}" if viewer == "buyer" else "Transféré"
    return {
        TransferState.DOSSARD_VERIFIE: "Vérifié",
        TransferState.EN_VENTE: "En vente",
        TransferState.RESERVE: "Réservé",
        TransferState.ELIGIBLE: "Réservé",
        TransferState.AUTORISE: "Réservé",
        TransferState.RESERVATION_EXPIREE: "Réservation expirée",
        TransferState.INELIGIBLE: "Non attribué",
        TransferState.REFUSE: "Transfert refusé",
    }[transfer.state]


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
    buyer = session.get(Buyer, transfer.buyer_id) if transfer.buyer_id else None
    verification = last_verification(session, transfer.id)
    # Le vendeur continue de suivre son dossard quand un achat s'arrête : il repart en vente.
    finished = (TransferState.VERSE, TransferState.REFUSE) if viewer == "seller" else FINAL_STATES
    context = {
        "transfer": transfer,
        "listing": listing,
        "race": session.get(Race, listing.race_id),
        "buyer": buyer,
        "viewer": viewer,
        "band": bib_band(transfer, viewer, buyer),
        "transferred": transfer.state in TRANSFERRED,
        "steps": STEPS,
        "reached": states.index(transfer.state) if transfer.state in states else None,
        "status": offer_status(transfer),
        "verification": verification,
        # Le justificatif se dépose une fois le dossard payé, et tant qu'il n'est pas accepté.
        "proof": proof_status(verification),
        "proof_open": transfer.state in PURCHASED and proof_status(verification) is not ProofStatus.FOURNI,
        "purchased": transfer.state in PURCHASED,
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
    except ProofNotExpected:
        return message(request, "Le justificatif se dépose une fois le dossard payé.", error=True)
    # La page se recharge et affiche le résultat.
    return Response(status_code=204, headers={"HX-Refresh": "true"})
