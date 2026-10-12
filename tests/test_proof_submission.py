"""Le justificatif de l'acheteur : un statut à part, qui ne bloque ni l'achat ni le paiement."""

from datetime import date

import pytest
from fakes import FakeClaude
from sqlmodel import select

from app.ai.client import AIError
from app.ai.verifier import DocumentType, ProofReading
from app.models import Buyer, LedgerEntry, Listing, Notification, ProofType, RaceRulesRow, Transfer, Verdict, Verification
from app.transfers import verification
from app.transfers.state import TransferState
from app.transfers.verification import (
    DecisionError,
    ProofNotExpected,
    ProofStatus,
    approve,
    last_verification,
    proof_status,
    refuse,
    submit_proof,
)

LICENCE = ProofReading(
    document_type=DocumentType.LICENCE_FFA, holder_name="MOREAU Léa", valid_until=date(2027, 8, 31), concern=None
)
DOUBTFUL = LICENCE.model_copy(update={"valid_until": None})


@pytest.fixture
def transfer(session, race):
    """Léa Moreau a acheté le dossard 42 : son paiement est autorisé. Elle n'a pas encore fourni de justificatif."""
    listing = Listing(race_id=race.id, bib_number=42, seller_email="vendeur@example.com", price_cents=4500)
    buyer = Buyer(race_id=race.id, first_name="Léa", last_name="Moreau", email="lea@example.com")
    session.add_all([listing, buyer])
    session.commit()
    transfer = Transfer(listing_id=listing.id, buyer_id=buyer.id, state=TransferState.AUTORISE)
    session.add(transfer)
    session.commit()
    return transfer


@pytest.fixture
def proof(tmp_path):
    path = tmp_path / "justificatif.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n une image")
    return path


def status(session, transfer) -> ProofStatus:
    return proof_status(last_verification(session, transfer.id))


def subjects(session) -> list[str]:
    return [subject.split(" — ")[0] for subject in session.exec(select(Notification.subject).order_by(Notification.id))]


def test_no_proof_yet_means_to_be_supplied(session, transfer):
    assert status(session, transfer) is ProofStatus.A_FOURNIR


def test_a_green_proof_is_supplied_and_the_file_is_deleted(session, transfer, proof):
    result = submit_proof(session, transfer, proof, client=FakeClaude(parsed=LICENCE))

    assert result.verdict is Verdict.VERT
    assert result.proof_type is ProofType.LICENCE_FFA
    assert status(session, transfer) is ProofStatus.FOURNI
    assert subjects(session) == ["Justificatif validé"]
    assert not proof.exists()


@pytest.mark.parametrize("reading", [LICENCE, DOUBTFUL])
def test_the_proof_never_moves_the_payment(session, transfer, proof, reading):
    submit_proof(session, transfer, proof, client=FakeClaude(parsed=reading))

    assert session.get(Transfer, transfer.id).state is TransferState.AUTORISE
    assert session.exec(select(LedgerEntry)).first() is None


@pytest.mark.parametrize("state", [TransferState.AUTORISE, TransferState.NOM_CHANGE, TransferState.ENCAISSE, TransferState.VERSE])
def test_the_proof_can_be_supplied_any_time_after_the_purchase(session, transfer, proof, state):
    transfer.state = state

    submit_proof(session, transfer, proof, client=FakeClaude(parsed=LICENCE))

    assert status(session, transfer) is ProofStatus.FOURNI
    assert session.get(Transfer, transfer.id).state is state


@pytest.mark.parametrize("state", [TransferState.EN_VENTE, TransferState.RESERVE, TransferState.ELIGIBLE, TransferState.REFUSE])
def test_no_proof_is_expected_before_the_bib_is_bought(session, transfer, proof, state):
    transfer.state = state
    claude = FakeClaude(parsed=LICENCE)

    with pytest.raises(ProofNotExpected):
        submit_proof(session, transfer, proof, client=claude)

    assert claude.calls == []
    assert not proof.exists()


def test_a_doubtful_proof_waits_for_the_organizer(session, transfer, proof):
    result = submit_proof(session, transfer, proof, client=FakeClaude(parsed=DOUBTFUL))

    assert result.verdict is Verdict.ORANGE
    assert status(session, transfer) is ProofStatus.A_VERIFIER
    assert subjects(session) == ["Justificatif à vérifier par l'organisateur"]
    assert not proof.exists()


def test_failed_analysis_saves_nothing_but_still_deletes_the_file(session, transfer, proof):
    with pytest.raises(AIError):
        submit_proof(session, transfer, proof, client=FakeClaude(stop_reason="refusal"))

    assert session.exec(select(Verification)).first() is None
    assert not proof.exists()


def test_oversized_file_is_refused_without_calling_the_ai(session, transfer, proof, monkeypatch):
    monkeypatch.setattr(verification, "MAX_PROOF_BYTES", 5)
    claude = FakeClaude(parsed=LICENCE)

    with pytest.raises(AIError, match="10 Mo"):
        submit_proof(session, transfer, proof, client=claude)

    assert claude.calls == []
    assert not proof.exists()


def test_race_rules_decide_which_proofs_are_accepted(session, race, transfer, proof):
    session.add(RaceRulesRow(race_id=race.id, required_proofs=[ProofType.PPS]))
    session.commit()

    result = submit_proof(session, transfer, proof, client=FakeClaude(parsed=LICENCE))

    assert result.verdict is Verdict.ROUGE
    assert "n'accepte pas ce justificatif" in result.explanation


def test_reseller_signals_do_not_cast_doubt_on_a_valid_proof(session, race, transfer, proof):
    # Le vendeur a trois annonces : c'est un signal pour l'organisateur, pas un défaut du justificatif de l'acheteur.
    session.add_all(
        Listing(race_id=race.id, bib_number=bib, seller_email="vendeur@example.com", price_cents=4000) for bib in (43, 44)
    )
    session.commit()

    assert submit_proof(session, transfer, proof, client=FakeClaude(parsed=LICENCE)).verdict is Verdict.VERT


# --- La décision de l'organisateur sur un justificatif douteux ---


def test_the_organizer_can_accept_a_doubtful_proof(session, transfer, proof):
    submit_proof(session, transfer, proof, client=FakeClaude(parsed=DOUBTFUL))

    approve(session, transfer)

    assert status(session, transfer) is ProofStatus.FOURNI
    assert subjects(session)[-1] == "Justificatif accepté par l'organisateur"


def test_a_proof_refused_by_the_organizer_is_to_be_supplied_again(session, transfer, proof):
    submit_proof(session, transfer, proof, client=FakeClaude(parsed=DOUBTFUL))

    refuse(session, transfer)

    assert status(session, transfer) is ProofStatus.A_FOURNIR
    assert subjects(session)[-1] == "Justificatif refusé : merci d'en déposer un autre"
    # Refuser un justificatif ne touche ni à l'achat ni à l'argent.
    assert session.get(Transfer, transfer.id).state is TransferState.AUTORISE
    assert session.exec(select(LedgerEntry)).first() is None


def test_a_new_proof_can_follow_a_refusal(session, transfer, proof, tmp_path):
    submit_proof(session, transfer, proof, client=FakeClaude(parsed=DOUBTFUL))
    refuse(session, transfer)
    second = tmp_path / "pps.png"
    second.write_bytes(b"\x89PNG\r\n\x1a\n une autre image")

    submit_proof(session, transfer, second, client=FakeClaude(parsed=LICENCE))

    assert status(session, transfer) is ProofStatus.FOURNI


def test_there_is_nothing_to_decide_without_a_doubtful_proof(session, transfer, proof):
    with pytest.raises(DecisionError):
        approve(session, transfer)  # aucun justificatif

    submit_proof(session, transfer, proof, client=FakeClaude(parsed=LICENCE))
    with pytest.raises(DecisionError):
        refuse(session, transfer)  # déjà au vert

    assert status(session, transfer) is ProofStatus.FOURNI
