from datetime import date

import pytest
from fakes import FakeClaude
from sqlmodel import select

from app.ai.client import AIError
from app.ai.verifier import DocumentType, ProofReading
from app.models import Listing, ProofType, RaceRulesRow, Transfer, Verdict, Verification, WaitlistEntry
from app.transfers import verification
from app.transfers.state import InvalidTransition, TransferState
from app.transfers.verification import submit_proof

LICENCE = ProofReading(
    document_type=DocumentType.LICENCE_FFA, holder_name="MOREAU Léa", valid_until=date(2027, 8, 31), concern=None
)


@pytest.fixture
def transfer(session, race):
    """Un transfert AUTORISE : Léa Moreau a payé le dossard 42, il ne manque que son justificatif."""
    listing = Listing(race_id=race.id, bib_number=42, seller_email="vendeur@example.com", price_cents=4500)
    buyer = WaitlistEntry(race_id=race.id, first_name="Léa", last_name="Moreau", email="lea@example.com")
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


def test_green_verdict_verifies_the_transfer_and_deletes_the_file(session, transfer, proof):
    result = submit_proof(session, transfer, proof, client=FakeClaude(parsed=LICENCE))

    assert result.verdict is Verdict.VERT
    assert result.proof_type is ProofType.LICENCE_FFA
    assert session.get(Transfer, transfer.id).state is TransferState.VERIFIE
    assert session.exec(select(Verification)).one().transfer_id == transfer.id
    assert not proof.exists()


def test_orange_verdict_waits_for_the_organizer(session, transfer, proof):
    doubtful = LICENCE.model_copy(update={"valid_until": None})

    result = submit_proof(session, transfer, proof, client=FakeClaude(parsed=doubtful))

    assert result.verdict is Verdict.ORANGE
    assert result.organizer_approved is None
    assert session.get(Transfer, transfer.id).state is TransferState.AUTORISE
    assert not proof.exists()


def test_red_verdict_waits_for_the_organizer(session, transfer, proof):
    someone_else = LICENCE.model_copy(update={"holder_name": "Karim BENALI"})

    result = submit_proof(session, transfer, proof, client=FakeClaude(parsed=someone_else))

    assert result.verdict is Verdict.ROUGE
    assert session.get(Transfer, transfer.id).state is TransferState.AUTORISE
    assert not proof.exists()


def test_failed_analysis_saves_nothing_but_still_deletes_the_file(session, transfer, proof):
    with pytest.raises(AIError):
        submit_proof(session, transfer, proof, client=FakeClaude(stop_reason="refusal"))

    assert session.exec(select(Verification)).first() is None
    assert session.get(Transfer, transfer.id).state is TransferState.AUTORISE
    assert not proof.exists()


def test_transfer_not_waiting_for_a_proof_is_refused_without_calling_the_ai(session, transfer, proof):
    transfer.state = TransferState.RESERVE
    claude = FakeClaude(parsed=LICENCE)

    with pytest.raises(InvalidTransition):
        submit_proof(session, transfer, proof, client=claude)

    assert claude.calls == []
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


def test_seller_with_many_listings_makes_a_perfect_proof_orange(session, race, transfer, proof):
    session.add_all(
        Listing(race_id=race.id, bib_number=bib, seller_email="vendeur@example.com", price_cents=4000)
        for bib in (43, 44)
    )
    session.commit()

    result = submit_proof(session, transfer, proof, client=FakeClaude(parsed=LICENCE))

    assert result.verdict is Verdict.ORANGE
    assert "Le compte vendeur a déposé 3 annonces" in result.explanation
    assert session.get(Transfer, transfer.id).state is TransferState.AUTORISE
