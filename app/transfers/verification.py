"""Le justificatif de l'acheteur (licence FFA ou PPS).

Il ne bloque pas l'achat : comme pour une inscription normale, il est à fournir plus tard, avant le
retrait du dossard. Son statut est donc indépendant du parcours de paiement : « à fournir », puis
« fourni » quand l'IA est au vert ou quand l'organisateur a accepté un justificatif douteux.
"""

from enum import StrEnum
from pathlib import Path

import anthropic
from sqlmodel import Session, select

from app.ai.client import AIError
from app.ai.verifier import assess, read_proof
from app.models import Buyer, Listing, Race, RaceRulesRow, Transfer, Verdict, Verification
from app.notifications import notify
from app.transfers.state import TransferState

MAX_PROOF_BYTES = 10 * 1024 * 1024

# Le justificatif se dépose une fois le dossard payé, jamais avant.
PURCHASED = (TransferState.AUTORISE, TransferState.NOM_CHANGE, TransferState.ENCAISSE, TransferState.VERSE)


class ProofStatus(StrEnum):
    A_FOURNIR = "A_FOURNIR"
    A_VERIFIER = "A_VERIFIER"  # déposé, mais l'IA a un doute : l'organisateur tranche
    FOURNI = "FOURNI"


class ProofNotExpected(Exception):
    """Ce transfert n'attend pas de justificatif : le dossard n'est pas (ou plus) acheté."""


class DecisionError(Exception):
    """Décision impossible. Le message peut être montré à l'organisateur."""


def last_verification(session: Session, transfer_id: int) -> Verification | None:
    statement = select(Verification).where(Verification.transfer_id == transfer_id).order_by(Verification.id.desc())
    return session.exec(statement).first()


def awaits_decision(verification: Verification | None) -> bool:
    """Vrai si l'IA n'était pas au vert et que l'organisateur n'a pas encore tranché."""
    return (
        verification is not None
        and verification.verdict is not Verdict.VERT
        and verification.organizer_approved is None
    )


def proof_status(verification: Verification | None) -> ProofStatus:
    """Où en est le justificatif de l'acheteur, d'après sa dernière analyse."""
    if verification is None or verification.organizer_approved is False:
        return ProofStatus.A_FOURNIR
    if verification.verdict is Verdict.VERT or verification.organizer_approved is True:
        return ProofStatus.FOURNI
    return ProofStatus.A_VERIFIER


def submit_proof(
    session: Session, transfer: Transfer, path: Path, client: anthropic.Anthropic | None = None
) -> Verification:
    """Analyse le justificatif déposé par l'acheteur et enregistre le verdict. L'état du transfert ne change pas.

    Le fichier est supprimé dans tous les cas, que l'analyse aboutisse ou non.
    """
    try:
        if transfer.state not in PURCHASED:
            raise ProofNotExpected(transfer.state)
        if path.stat().st_size > MAX_PROOF_BYTES:
            raise AIError("Le fichier dépasse 10 Mo.")

        listing = session.get(Listing, transfer.listing_id)
        race = session.get(Race, listing.race_id)
        buyer = session.get(Buyer, transfer.buyer_id)
        rules = session.exec(select(RaceRulesRow).where(RaceRulesRow.race_id == race.id)).first()

        reading = read_proof(path.read_bytes(), client)
        assessment = assess(
            reading,
            buyer_name=f"{buyer.first_name} {buyer.last_name}",
            race_date=race.event_date,
            accepted_proofs=rules.required_proofs if rules else None,
        )
        verification = Verification(
            transfer_id=transfer.id,
            proof_type=assessment.proof_type,
            verdict=assessment.verdict,
            explanation=assessment.explanation,
        )
        session.add(verification)
        link = f"/runner/transfers/{transfer.id}"
        if assessment.verdict is Verdict.VERT:
            notify(
                session,
                buyer.email,
                f"Justificatif validé — {race.name}",
                "Votre justificatif est accepté : vous n'avez plus rien à fournir avant le retrait du dossard.",
                link=link,
            )
        else:
            notify(
                session,
                buyer.email,
                f"Justificatif à vérifier par l'organisateur — {race.name}",
                f"{assessment.explanation} L'organisateur va trancher. Votre achat n'est pas remis en cause.",
                link=link,
            )
        session.commit()
        session.refresh(verification)
        return verification
    finally:
        path.unlink(missing_ok=True)


# --- La décision de l'organisateur sur un justificatif douteux ---


def _decide(session: Session, transfer: Transfer, approved: bool) -> Verification:
    verification = last_verification(session, transfer.id)
    if not awaits_decision(verification):
        raise DecisionError("Ce justificatif n'attend pas de décision.")
    verification.organizer_approved = approved
    session.add(verification)
    return verification


def approve(session: Session, transfer: Transfer) -> None:
    """L'organisateur accepte un justificatif que l'IA n'avait pas mis au vert : il est « fourni »."""
    _decide(session, transfer, approved=True)
    listing = session.get(Listing, transfer.listing_id)
    notify(
        session,
        session.get(Buyer, transfer.buyer_id).email,
        f"Justificatif accepté par l'organisateur — {session.get(Race, listing.race_id).name}",
        "L'organisateur a accepté votre justificatif : vous n'avez plus rien à fournir avant le retrait du dossard.",
        link=f"/runner/transfers/{transfer.id}",
    )
    session.commit()


def refuse(session: Session, transfer: Transfer) -> None:
    """L'organisateur refuse le justificatif : il reste « à fournir ». L'achat et le paiement ne bougent pas."""
    _decide(session, transfer, approved=False)
    listing = session.get(Listing, transfer.listing_id)
    notify(
        session,
        session.get(Buyer, transfer.buyer_id).email,
        f"Justificatif refusé : merci d'en déposer un autre — {session.get(Race, listing.race_id).name}",
        "L'organisateur n'a pas accepté votre justificatif. Déposez une licence FFA ou un PPS valide avant "
        "le retrait du dossard. Votre achat n'est pas remis en cause.",
        link=f"/runner/transfers/{transfer.id}",
    )
    session.commit()
