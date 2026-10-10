"""Tests de l'IA simulée (AI_MODE=fake) : ses réponses préenregistrées sont celles qu'on attend de la vraie IA."""

import dataclasses

import pytest
from fakes import FakeClaude
from paypal_fakes import reserved_transfer
from sqlmodel import select
from test_ai_live import EXPECTED_PROOFS, EXPECTED_RULES, SEED, check_proof, check_rules

from app.ai import client, rules_reader, verifier
from app.ai.client import AIError
from app.ai.rules_reader import read_rules
from app.ai.verifier import read_proof
from app.config import settings
from app.models import RaceRulesRow, Transfer, WaitlistEntry
from app.templating import templates
from app.transfers.state import TransferState


@pytest.fixture
def fake_ai(monkeypatch):
    """Passe en IA simulée, sans clé, et fait échouer toute tentative de joindre Claude."""
    simulated = dataclasses.replace(settings, ai_mode="fake", anthropic_api_key="")
    monkeypatch.setattr(rules_reader, "settings", simulated)
    monkeypatch.setattr(verifier, "settings", simulated)
    monkeypatch.setitem(templates.env.globals, "ai_mode", "fake")

    def no_network():
        raise AssertionError("L'IA simulée ne doit pas appeler Claude")

    monkeypatch.setattr(client, "get_client", no_network)


@pytest.mark.parametrize("name", EXPECTED_RULES)
def test_simulated_rules_are_the_ones_expected_from_the_real_ai(fake_ai, name):
    check_rules(name, read_rules((SEED / "reglements" / f"{name}.pdf").read_bytes()))


@pytest.mark.parametrize("name", EXPECTED_PROOFS)
def test_simulated_proof_readings_are_the_ones_expected_from_the_real_ai(fake_ai, name):
    check_proof(name, read_proof((SEED / "justificatifs" / name).read_bytes()))


def test_a_file_that_is_not_a_seed_example_is_refused(fake_ai):
    with pytest.raises(AIError, match="IA simulée : seuls les fichiers d'exemple de seed/reglements/"):
        read_rules(b"%PDF-1.4 un autre reglement")
    with pytest.raises(AIError, match="IA simulée : seuls les fichiers d'exemple de seed/justificatifs/"):
        read_proof(b"\x89PNG\r\n\x1a\n une autre image")


def test_an_explicit_client_is_still_used(fake_ai):
    # Les tests qui passent leur propre faux Claude ne sont pas court-circuités par le mode simulé.
    claude = FakeClaude(parsed=read_proof((SEED / "justificatifs" / "pps-karim-benali.pdf").read_bytes()))

    read_proof(b"\x89PNG\r\n\x1a\n une autre image", client=claude)

    assert len(claude.calls) == 1


def test_pages_say_when_the_ai_is_simulated(client, fake_ai, monkeypatch):
    assert "IA simulée" in client.get("/").text

    monkeypatch.setitem(templates.env.globals, "ai_mode", "real")

    assert "IA simulée" not in client.get("/").text


def test_organizer_can_read_a_seed_reglement_without_claude(client, session, race, fake_ai):
    pdf = (SEED / "reglements" / "marathon-deux-rives.pdf").read_bytes()

    response = client.post(f"/organizer/races/{race.id}/rules/extract", files={"reglement": ("r.pdf", pdf, "application/pdf")})

    assert response.status_code == 200
    assert '<option value="false" selected>Interdit</option>' in response.text
    assert "Tout transfert, revente ou cession de dossard à un tiers est interdit" in response.text
    assert session.exec(select(RaceRulesRow)).first() is None  # rien n'est enregistré avant la confirmation


def test_buyer_can_be_verified_with_a_seed_proof_without_claude(client, session, race, fake_ai):
    transfer = reserved_transfer(session, race)
    buyer = session.get(WaitlistEntry, transfer.buyer_id)
    buyer.first_name, buyer.last_name = "Léa", "Moreau"
    transfer.state = TransferState.AUTORISE
    session.add_all([buyer, transfer])
    session.commit()
    licence = (SEED / "justificatifs" / "licence-ffa-lea-moreau.png").read_bytes()

    response = client.post(
        f"/runner/transfers/{transfer.id}/proof", files={"justificatif": ("licence.png", licence, "image/png")}
    )

    assert response.status_code == 204
    assert session.get(Transfer, transfer.id).state is TransferState.VERIFIE
