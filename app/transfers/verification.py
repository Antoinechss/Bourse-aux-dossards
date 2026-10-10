"""Validation d'un transfert : automatique si l'IA est au vert, sinon c'est l'organisateur qui tranche."""

from pathlib import Path

import anthropic
from sqlmodel import Session, select

from app.ai.client import AIError
from app.ai.verifier import account_signals, assess, read_proof
from app.models import Listing, Race, RaceRulesRow, Transfer, Verdict, Verification, WaitlistEntry
from app.notifications import notify
from app.payments import service
from app.transfers.flow import LOCK
from app.transfers.state import InvalidTransition, TransferEvent, TransferState, next_state

MAX_PROOF_BYTES = 10 * 1024 * 1024


def submit_proof(
    session: Session, transfer: Transfer, path: Path, client: anthropic.Anthropic | None = None
) -> Verification:
    """Analyse le justificatif déposé pour ce transfert et enregistre le verdict.

    Vert : le transfert passe à VERIFIE. Orange ou rouge : il reste AUTORISE, à l'organisateur de trancher.
    Le fichier est supprimé dans tous les cas, que l'analyse aboutisse ou non.
    """
    try:
        # Lève InvalidTransition si le transfert n'attend pas de justificatif : il doit être AUTORISE.
        verified = next_state(transfer.state, TransferEvent.VERIFIER)
        if path.stat().st_size > MAX_PROOF_BYTES:
            raise AIError("Le fichier dépasse 10 Mo.")

        listing = session.get(Listing, transfer.listing_id)
        race = session.get(Race, listing.race_id)
        buyer = session.get(WaitlistEntry, transfer.buyer_id)
        rules = session.exec(select(RaceRulesRow).where(RaceRulesRow.race_id == race.id)).first()

        reading = read_proof(path.read_bytes(), client)
        assessment = assess(
            reading,
            buyer_name=f"{buyer.first_name} {buyer.last_name}",
            race_date=race.event_date,
            accepted_proofs=rules.required_proofs if rules else None,
            seller=account_signals(session, listing.seller_email),
            buyer=account_signals(session, buyer.email),
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
            transfer.state = verified
            session.add(transfer)
            notify(
                session,
                buyer.email,
                f"Justificatif validé — {race.name}",
                "Votre justificatif est accepté. L'organisateur met le dossard à votre nom, puis votre "
                "paiement sera encaissé.",
                link=link,
            )
        else:
            notify(
                session,
                buyer.email,
                f"Justificatif à vérifier par l'organisateur — {race.name}",
                f"{assessment.explanation} L'organisateur va trancher. Vous n'êtes pas débité tant que "
                "le transfert n'est pas validé.",
                link=link,
            )
        # Le verdict et le changement d'état sont enregistrés ensemble, ou pas du tout.
        session.commit()
        session.refresh(verification)
        return verification
    finally:
        path.unlink(missing_ok=True)


# --- La décision de l'organisateur, quand l'IA n'est pas au vert ---


class DecisionError(Exception):
    """Décision impossible là où en est le transfert. Le message peut être montré à l'organisateur."""


def last_verification(session: Session, transfer_id: int) -> Verification | None:
    statement = select(Verification).where(Verification.transfer_id == transfer_id).order_by(Verification.id.desc())
    return session.exec(statement).first()


def awaits_decision(transfer: Transfer, verification: Verification | None) -> bool:
    """Vrai si l'IA n'était pas au vert et que l'organisateur n'a pas encore tranché."""
    return (
        transfer.state is TransferState.AUTORISE
        and verification is not None
        and verification.verdict is not Verdict.VERT
        and verification.organizer_approved is None
    )


def approve(session: Session, transfer: Transfer) -> None:
    """L'organisateur valide un transfert que l'IA n'avait pas mis au vert : AUTORISE → VERIFIE.

    La suite (nom, encaissement, versement) est faite par le ménage périodique, comme après un verdict vert.
    """
    with LOCK:
        session.refresh(transfer)
        verification = last_verification(session, transfer.id)
        if not awaits_decision(transfer, verification):
            raise DecisionError("Ce transfert n'attend pas de validation.")
        transfer.state = next_state(transfer.state, TransferEvent.VERIFIER)
        verification.organizer_approved = True
        listing = session.get(Listing, transfer.listing_id)
        notify(
            session,
            session.get(WaitlistEntry, transfer.buyer_id).email,
            f"Transfert validé par l'organisateur — {session.get(Race, listing.race_id).name}",
            "L'organisateur a accepté votre justificatif. Il met le dossard à votre nom, puis votre paiement "
            "sera encaissé.",
            link=f"/runner/transfers/{transfer.id}",
        )
        session.add_all([transfer, verification])
        session.commit()


def refuse(session: Session, transfer: Transfer) -> None:
    """L'organisateur refuse le transfert : l'autorisation PayPal est annulée, puis AUTORISE → REFUSE.

    L'acheteur n'est jamais débité. Lève PayPalError si l'annulation échoue : l'état ne change alors pas.
    """
    with LOCK:
        session.refresh(transfer)
        try:
            refused = next_state(transfer.state, TransferEvent.REFUSER)
        except InvalidTransition:
            raise DecisionError("Seul un transfert dont le paiement est autorisé peut être refusé.") from None
        try:
            # L'argent d'abord : on ne déclare le transfert refusé qu'une fois les fonds libérés.
            service.void(session, transfer)
        except service.PaymentError as error:
            raise DecisionError(str(error)) from error
        transfer.state = refused
        session.add(transfer)
        verification = last_verification(session, transfer.id)
        if verification is not None and verification.organizer_approved is None:
            verification.organizer_approved = False
            session.add(verification)
        listing = session.get(Listing, transfer.listing_id)
        race = session.get(Race, listing.race_id)
        notify(
            session,
            session.get(WaitlistEntry, transfer.buyer_id).email,
            f"Transfert refusé : vous n'êtes pas débité — {race.name}",
            f"L'organisateur a refusé le transfert du dossard n°{listing.bib_number}. L'autorisation de paiement "
            "est annulée : rien n'a été prélevé.",
            link=f"/runner/transfers/{transfer.id}",
        )
        notify(
            session,
            listing.seller_email,
            f"Transfert refusé — dossard n°{listing.bib_number}",
            "L'organisateur a refusé l'acheteur proposé, qui n'a pas été débité. Vous pouvez remettre votre "
            "dossard en vente.",
            link=f"/runner/listings/{listing.id}",
        )
        session.commit()
