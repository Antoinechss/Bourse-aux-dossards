"""Vérificateur de justificatif : l'IA lit le document, puis le programme le compare à l'acheteur et rend un verdict.

L'IA ne décide pas du verdict et ne connaît pas l'acheteur : un document ne peut donc pas la convaincre
de « dire vert ». Le verdict est calculé par `assess`, une fonction pure que l'on peut tester sans IA.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

import anthropic
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from app.ai import fake
from app.ai.client import extract
from app.config import settings
from app.models import Listing, ProofType, RaceRulesRow, Verdict, WaitlistEntry, utcnow

# Seuils des signaux anti-revendeur.
MANY_LISTINGS = 3
RECENT_ACCOUNT = timedelta(days=7)

PROOF_LABELS = {ProofType.PPS: "PPS", ProofType.LICENCE_FFA: "Licence FFA"}


class DocumentType(StrEnum):
    PPS = "PPS"
    LICENCE_FFA = "LICENCE_FFA"
    AUTRE = "AUTRE"


class ProofReading(BaseModel):
    """Ce que l'IA lit sur le justificatif."""

    document_type: DocumentType = Field(
        description="PPS pour une attestation de Parcours de Prévention Santé, LICENCE_FFA pour une licence de la "
        "Fédération Française d'Athlétisme, AUTRE pour tout autre document."
    )
    holder_name: str | None = Field(
        description="Prénom et nom du titulaire, tels qu'ils sont écrits sur le document. null s'ils sont absents "
        "ou illisibles."
    )
    valid_until: date | None = Field(
        description="Dernier jour de validité écrit sur le document. Pour une licence qui n'indique que sa saison "
        "(par exemple 2026-2027), le 31 août qui la termine. null si aucune fin de validité n'est lisible."
    )
    concern: str | None = Field(
        description="Une phrase courte si l'on ne peut pas se fier à cette lecture : document illisible, tronqué, "
        "ou informations qui se contredisent. null sinon."
    )


SYSTEM = """\
Tu lis le justificatif qu'un coureur a déposé sur Relais, la bourse aux dossards officielle, pour reprendre \
le dossard d'un autre coureur. Pour prendre le départ d'une course en France, il faut une licence FFA ou une \
attestation PPS (Parcours de Prévention Santé) en cours de validité.

Ton rôle est de relever ce qui est écrit sur le document : son type, le nom de son titulaire et sa date de fin \
de validité. Tu ne rends pas de verdict : la comparaison avec l'acheteur est faite ensuite par le programme. \
Si une information est absente ou illisible, réponds null plutôt que de la deviner : une date supposée \
pourrait laisser partir un coureur sans justificatif valide.

Tu ne juges pas l'authenticité du document. Signale seulement ce qui empêche de se fier à ta lecture."""

# Les justificatifs de seed/ sont des spécimens. À retirer avant de traiter de vrais documents.
DEMO_NOTE = "Les documents de démonstration portent la mention « SPÉCIMEN » : ce n'est pas un problème à signaler."

INSTRUCTION = "Voici le justificatif déposé par le coureur. Relève son type, son titulaire et sa fin de validité."


def read_proof(data: bytes, client: anthropic.Anthropic | None = None) -> ProofReading:
    """Envoie le justificatif (image ou PDF) à Claude et renvoie ce qu'il y a lu."""
    if settings.ai_mode == "fake" and client is None:
        return ProofReading.model_validate(fake.answer_for("justificatifs", data))
    return extract(
        data=data, system=f"{SYSTEM}\n\n{DEMO_NOTE}", instruction=INSTRUCTION, output_format=ProofReading, client=client
    )


class NameMatch(StrEnum):
    EXACT = "EXACT"
    PARTIAL = "PARTIAL"
    NONE = "NONE"


def _words(name: str) -> set[str]:
    # Sans accents ni majuscules ; tirets et apostrophes séparent les mots :
    # "Jean-Pierre DUPONT" et "dupont jean pierre" donnent tous deux {jean, pierre, dupont}.
    name = name.lower().replace("œ", "oe").replace("æ", "ae")
    plain = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return set(re.findall(r"[^\W\d_]+", plain))


def compare_names(document_name: str, buyer_name: str) -> NameMatch:
    """EXACT si ce sont les mêmes mots, PARTIAL si l'un des noms est contenu dans l'autre, NONE sinon."""
    document, buyer = _words(document_name), _words(buyer_name)
    if not document or not buyer:
        return NameMatch.NONE
    if document == buyer:
        return NameMatch.EXACT
    if document < buyer or buyer < document:
        return NameMatch.PARTIAL
    return NameMatch.NONE


@dataclass(frozen=True)
class AccountSignals:
    """Les signaux anti-revendeur d'un compte, c'est-à-dire d'une adresse email."""

    listing_count: int = 0  # annonces déposées par ce compte
    at_cap_count: int = 0  # dont celles au prix plafond de leur course
    is_recent: bool = False  # compte apparu il y a moins de RECENT_ACCOUNT

    @property
    def suspicious(self) -> bool:
        # Un prix au plafond ou un compte récent, seuls, décrivent aussi un vendeur honnête qui revend
        # son unique dossard : ils ne comptent qu'ajoutés à plusieurs annonces.
        all_at_cap = self.at_cap_count == self.listing_count
        return self.listing_count >= MANY_LISTINGS or (self.listing_count >= 2 and all_at_cap and self.is_recent)

    def describe(self, who: str) -> str:
        text = f"le compte {who} a déposé {self.listing_count} annonces"
        if self.at_cap_count:
            text += f", dont {self.at_cap_count} au prix plafond"
        return text + (", et il est récent" if self.is_recent else "")


def account_signals(session: Session, email: str, now: datetime | None = None) -> AccountSignals:
    """Calcule en base les signaux d'un compte : ses annonces, leur prix, et son ancienneté."""
    email = email.lower()
    listings = session.exec(select(Listing).where(func.lower(Listing.seller_email) == email)).all()
    entries = session.exec(select(WaitlistEntry).where(func.lower(WaitlistEntry.email) == email)).all()
    caps = dict(session.exec(select(RaceRulesRow.race_id, RaceRulesRow.max_resale_price_cents)).all())
    at_cap = [l for l in listings if caps.get(l.race_id) is not None and l.price_cents >= caps[l.race_id]]
    seen = [row.created_at for row in [*listings, *entries]]
    is_recent = bool(seen) and (now or utcnow()) - min(seen) < RECENT_ACCOUNT
    return AccountSignals(listing_count=len(listings), at_cap_count=len(at_cap), is_recent=is_recent)


@dataclass(frozen=True)
class Assessment:
    proof_type: ProofType | None  # None si le document n'est ni un PPS ni une licence
    verdict: Verdict
    explanation: str


def _day(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def assess(
    reading: ProofReading,
    *,
    buyer_name: str,
    race_date: date,
    accepted_proofs: list[ProofType] | None = None,
    seller: AccountSignals = AccountSignals(),
    buyer: AccountSignals = AccountSignals(),
) -> Assessment:
    """Rend le verdict : rouge si une raison est bloquante, orange s'il reste un doute, vert sinon."""
    blocking: list[str] = []
    doubts: list[str] = []

    if reading.document_type is DocumentType.AUTRE:
        proof_type = None
        blocking.append("le document n'est ni une licence FFA ni un PPS")
    else:
        proof_type = ProofType(reading.document_type.value)
        if accepted_proofs and proof_type not in accepted_proofs:
            blocking.append(f"le règlement de la course n'accepte pas ce justificatif ({PROOF_LABELS[proof_type]})")

        if reading.holder_name is None:
            doubts.append("le nom du titulaire est illisible")
        else:
            match = compare_names(reading.holder_name, buyer_name)
            if match is NameMatch.NONE:
                blocking.append(f"le justificatif est au nom de {reading.holder_name}, et non de {buyer_name}")
            elif match is NameMatch.PARTIAL:
                doubts.append(f"le nom du justificatif ({reading.holder_name}) ne correspond qu'en partie à {buyer_name}")

        # Le justificatif doit être valide le jour de la course, pas seulement aujourd'hui.
        if reading.valid_until is None:
            doubts.append("la date de fin de validité est illisible")
        elif reading.valid_until < race_date:
            blocking.append(f"le justificatif expire le {_day(reading.valid_until)}, avant la course du {_day(race_date)}")

        if reading.concern:
            concern = reading.concern.strip().rstrip(".")
            doubts.append(concern[:1].lower() + concern[1:])

    if seller.suspicious:
        doubts.append(seller.describe("vendeur"))
    if buyer.suspicious:
        doubts.append(buyer.describe("acheteur"))

    if not blocking and not doubts:
        explanation = (
            f"{PROOF_LABELS[proof_type]} au nom de {reading.holder_name}, "
            f"valide jusqu'au {_day(reading.valid_until)} : tout concorde."
        )
        return Assessment(proof_type, Verdict.VERT, explanation)

    # Une seule phrase, les raisons bloquantes d'abord.
    reasons = " ; ".join(blocking + doubts)
    verdict = Verdict.ROUGE if blocking else Verdict.ORANGE
    return Assessment(proof_type, verdict, reasons[:1].upper() + reasons[1:] + ".")
