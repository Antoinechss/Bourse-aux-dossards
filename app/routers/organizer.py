"""Espace de l'organisateur : tableau de bord des transferts, décisions, et règlement de chaque course."""

import logging

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app.ai.client import AIError
from app.ai.rules_reader import clean, read_rules
from app.db import SessionDep
from app.forms import parse_euros
from app.models import (
    LedgerEntry,
    LedgerKind,
    Listing,
    ProofType,
    Race,
    RaceRules,
    RaceRulesRow,
    Transfer,
    Verdict,
    Verification,
    WaitlistEntry,
)
from app.payments.client import PayPalError
from app.payments.commission import compute_amounts
from app.templating import euros, templates
from app.transfers.state import TransferState
from app.transfers.verification import DecisionError, approve, awaits_decision, refuse

router = APIRouter(prefix="/organizer", tags=["organizer"])
logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 20 * 1024 * 1024


def parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        raise ValueError(f"Date illisible : « {text} ».") from None


class RulesForm(BaseModel):
    """Le formulaire de l'écran de confirmation : tout y est du texte, comme dans la page."""

    transfer_allowed: str = ""  # "true", "false" ou vide pour « non précisé »
    transfer_deadline: str = ""
    max_resale_price: str = ""  # en euros
    transfer_fee: str = ""  # en euros
    required_proofs: list[ProofType] = []
    transfer_allowed_quote: str = ""
    transfer_deadline_quote: str = ""
    max_resale_price_quote: str = ""
    transfer_fee_quote: str = ""
    required_proofs_quote: str = ""

    @classmethod
    def from_rules(cls, rules: RaceRules) -> "RulesForm":
        return cls(
            transfer_allowed="" if rules.transfer_allowed is None else str(rules.transfer_allowed).lower(),
            transfer_deadline=rules.transfer_deadline.isoformat() if rules.transfer_deadline else "",
            max_resale_price="" if rules.max_resale_price_cents is None else euros(rules.max_resale_price_cents),
            transfer_fee="" if rules.transfer_fee_cents is None else euros(rules.transfer_fee_cents),
            required_proofs=rules.required_proofs or [],
            transfer_allowed_quote=rules.transfer_allowed_quote or "",
            transfer_deadline_quote=rules.transfer_deadline_quote or "",
            max_resale_price_quote=rules.max_resale_price_quote or "",
            transfer_fee_quote=rules.transfer_fee_quote or "",
            required_proofs_quote=rules.required_proofs_quote or "",
        )

    def to_rules(self) -> RaceRules:
        """Lève ValueError, avec un message à montrer à l'organisateur, si une saisie est illisible."""
        rules = RaceRules(
            transfer_allowed={"true": True, "false": False}.get(self.transfer_allowed),
            transfer_allowed_quote=self.transfer_allowed_quote,
            transfer_deadline=parse_date(self.transfer_deadline),
            transfer_deadline_quote=self.transfer_deadline_quote,
            max_resale_price_cents=parse_euros(self.max_resale_price),
            max_resale_price_quote=self.max_resale_price_quote,
            transfer_fee_cents=parse_euros(self.transfer_fee),
            transfer_fee_quote=self.transfer_fee_quote,
            required_proofs=self.required_proofs,
            required_proofs_quote=self.required_proofs_quote,
        )
        # Une règle saisie à la main par l'organisateur n'a pas de citation : c'est permis.
        return clean(rules, require_quote=False)


def get_race(session: Session, race_id: int) -> Race:
    race = session.get(Race, race_id)
    if race is None:
        raise HTTPException(status_code=404, detail="Course inconnue")
    return race


def saved_rules(session: Session, race_id: int) -> RaceRulesRow | None:
    return session.exec(select(RaceRulesRow).where(RaceRulesRow.race_id == race_id)).first()


@router.get("", response_class=HTMLResponse)
def dashboard_page(request: Request, session: SessionDep):
    """Le tableau de bord. La table et les indicateurs sont remplis par dashboard.js, depuis /organizer/api/dashboard."""
    races = session.exec(select(Race).order_by(Race.event_date)).all()
    with_rules = set(session.exec(select(RaceRulesRow.race_id)).all())
    return templates.TemplateResponse(request, "organizer/dashboard.html", {"races": races, "with_rules": with_rules})


@router.get("/races/{race_id}/rules", response_class=HTMLResponse)
def rules_page(race_id: int, request: Request, session: SessionDep, saved: bool = False):
    context = {"race": get_race(session, race_id), "rules": saved_rules(session, race_id), "saved": saved}
    return templates.TemplateResponse(request, "organizer/rules.html", context)


@router.post("/races/{race_id}/rules/extract", response_class=HTMLResponse)
def extract_rules(race_id: int, request: Request, session: SessionDep, reglement: UploadFile):
    race = get_race(session, race_id)
    pdf = reglement.file.read(MAX_PDF_BYTES + 1)
    try:
        if len(pdf) > MAX_PDF_BYTES:
            raise AIError("Le fichier dépasse 20 Mo.")
        rules = read_rules(pdf)
    except AIError as error:
        context = {"race": race, "rules": saved_rules(session, race_id), "error": str(error)}
        return templates.TemplateResponse(request, "organizer/rules.html", context, status_code=422)
    # Rien n'est enregistré ici : les règles lues voyagent dans le formulaire jusqu'à la confirmation.
    context = {"race": race, "form": RulesForm.from_rules(rules)}
    return templates.TemplateResponse(request, "organizer/rules_confirm.html", context)


@router.post("/races/{race_id}/rules")
def confirm_rules(race_id: int, request: Request, session: SessionDep, form: Annotated[RulesForm, Form()]):
    race = get_race(session, race_id)
    try:
        rules = form.to_rules()
    except ValueError as error:
        context = {"race": race, "form": form, "error": str(error)}
        return templates.TemplateResponse(request, "organizer/rules_confirm.html", context, status_code=422)
    row = saved_rules(session, race_id) or RaceRulesRow(race_id=race_id)
    row.sqlmodel_update(rules.model_dump())
    session.add(row)
    session.commit()
    # 303 : le navigateur recharge la page en GET, donc un rafraîchissement ne renvoie pas le formulaire.
    return RedirectResponse(f"/organizer/races/{race_id}/rules?saved=true", status_code=303)


# --- Le tableau de bord des transferts ---

STATE_LABELS = {
    TransferState.EN_VENTE: "En vente",
    TransferState.RESERVE: "Réservé",
    TransferState.AUTORISE: "Autorisé",
    TransferState.VERIFIE: "Vérifié",
    TransferState.NOM_CHANGE: "Nom changé",
    TransferState.ENCAISSE: "Encaissé",
    TransferState.VERSE: "Versé",
    TransferState.REFUSE: "Refusé",
    TransferState.OFFRE_EXPIREE: "Offre expirée",
}
# À partir de ces états, le dossard est au nom de son nouveau coureur.
REASSIGNED = (TransferState.NOM_CHANGE, TransferState.ENCAISSE, TransferState.VERSE)


def decision_label(transfer: Transfer, verification: Verification | None) -> str:
    """Qui a validé ou refusé ce transfert, en clair."""
    if verification is None:
        return "Justificatif attendu" if transfer.state is TransferState.AUTORISE else ""
    if verification.organizer_approved is True:
        return "Validé par l'organisateur"
    if verification.organizer_approved is False:
        return "Refusé par l'organisateur"
    return "Validation automatique" if verification.verdict is Verdict.VERT else "À décider"


def indicators(session: Session, transfers: list[Transfer]) -> dict:
    listed = len(session.exec(select(Listing.id)).all())
    reassigned = sum(1 for transfer in transfers if transfer.state in REASSIGNED)
    # La commission n'est « reversée » qu'une fois le versement confirmé par PayPal : transfert à l'état VERSE.
    paid = {transfer.id for transfer in transfers if transfer.state is TransferState.VERSE}
    payouts = session.exec(select(LedgerEntry).where(LedgerEntry.kind == LedgerKind.VERSEMENT_ORGANISATEUR)).all()
    return {
        "reassigned": reassigned,
        "listed": listed,
        # Part des dossards mis en vente qui ont retrouvé un coureur, arrondie sans passer par un float.
        "saved_percent": (100 * reassigned + listed // 2) // listed if listed else None,
        "organizer_commission_cents": sum(entry.amount_cents for entry in payouts if entry.transfer_id in paid),
    }


@router.get("/api/dashboard")
def dashboard_data(session: SessionDep) -> dict:
    """Les indicateurs et une ligne par transfert, du plus récent au plus ancien, pour la table AG Grid."""
    transfers = session.exec(select(Transfer).order_by(Transfer.id.desc())).all()
    listings = {listing.id: listing for listing in session.exec(select(Listing))}
    races = {race.id: race for race in session.exec(select(Race))}
    buyers = {entry.id: entry for entry in session.exec(select(WaitlistEntry))}
    # Le dernier verdict de chaque transfert : les suivants remplacent les précédents.
    verifications = {v.transfer_id: v for v in session.exec(select(Verification).order_by(Verification.id))}

    rows = []
    for transfer in transfers:
        listing = listings[transfer.listing_id]
        race = races[listing.race_id]
        buyer = buyers.get(transfer.buyer_id)
        verification = verifications.get(transfer.id)
        amounts = compute_amounts(listing.price_cents, race.platform_fee_bps, race.organizer_fee_bps)
        rows.append(
            {
                "id": transfer.id,
                "race": race.name,
                "bib_number": listing.bib_number,
                "state": transfer.state,
                "state_label": STATE_LABELS[transfer.state],
                "buyer": f"{buyer.first_name} {buyer.last_name}" if buyer else None,
                "price_cents": amounts.price_cents,
                "organizer_fee_cents": amounts.organizer_fee_cents,
                "total_cents": amounts.total_cents,
                "verdict": verification.verdict if verification else None,
                "explanation": verification.explanation if verification else None,
                "decision": decision_label(transfer, verification),
                "can_approve": awaits_decision(transfer, verification),
                "can_refuse": transfer.state is TransferState.AUTORISE,
            }
        )
    return {"indicators": indicators(session, transfers), "transfers": rows}


def get_transfer(session: Session, transfer_id: int) -> Transfer:
    transfer = session.get(Transfer, transfer_id)
    if transfer is None:
        raise HTTPException(status_code=404, detail="Transfert inconnu")
    return transfer


@router.post("/api/transfers/{transfer_id}/approve")
def approve_transfer(transfer_id: int, session: SessionDep) -> dict:
    transfer = get_transfer(session, transfer_id)
    try:
        approve(session, transfer)
    except DecisionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"state": transfer.state}


@router.post("/api/transfers/{transfer_id}/refuse")
def refuse_transfer(transfer_id: int, session: SessionDep) -> dict:
    transfer = get_transfer(session, transfer_id)
    try:
        refuse(session, transfer)
    except DecisionError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except PayPalError as error:
        logger.error("Annulation refusée par PayPal pour le transfert %s : %s", transfer_id, error)
        detail = "PayPal n'a pas pu annuler l'autorisation : le transfert n'est pas refusé. Réessayez."
        raise HTTPException(status_code=502, detail=detail) from error
    return {"state": transfer.state}
