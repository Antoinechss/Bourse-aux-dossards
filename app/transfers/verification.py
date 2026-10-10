"""Dépôt d'un justificatif : on l'analyse, on enregistre le verdict, et le transfert avance si l'IA est au vert."""

from pathlib import Path

import anthropic
from sqlmodel import Session, select

from app.ai.client import AIError
from app.ai.verifier import account_signals, assess, read_proof
from app.models import Listing, Race, RaceRulesRow, Transfer, Verdict, Verification, WaitlistEntry
from app.notifications import notify
from app.transfers.state import TransferEvent, next_state

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
