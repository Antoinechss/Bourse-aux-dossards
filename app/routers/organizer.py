"""Pages de l'organisateur : déposer le règlement d'une course, relire puis confirmer les règles extraites."""

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
from app.models import ProofType, Race, RaceRules, RaceRulesRow
from app.templating import euros, templates

router = APIRouter(prefix="/organizer", tags=["organizer"])

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
def races_page(request: Request, session: SessionDep):
    races = session.exec(select(Race).order_by(Race.event_date)).all()
    with_rules = set(session.exec(select(RaceRulesRow.race_id)).all())
    return templates.TemplateResponse(request, "organizer/races.html", {"races": races, "with_rules": with_rules})


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
