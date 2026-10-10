from datetime import date

import pytest
from sqlmodel import select

from app.ai.client import AIError
from app.models import ProofType, RaceRules, RaceRulesRow
from app.routers import organizer
from app.routers.organizer import parse_euros

PDF = ("reglement.pdf", b"%PDF-1.4 faux reglement", "application/pdf")

FOUND = RaceRules(
    transfer_allowed=True,
    transfer_allowed_quote="Article 9 — Le transfert de dossard est autorisé.",
    transfer_deadline=date(2027, 3, 28),
    transfer_deadline_quote="Article 9 — Aucun transfert n'est accepté après le 28 mars 2027.",
    max_resale_price_cents=4500,
    max_resale_price_quote="Article 9 — Le prix de revente ne peut excéder 45 €.",
    transfer_fee_cents=None,
    transfer_fee_quote=None,
    required_proofs=[ProofType.PPS, ProofType.LICENCE_FFA],
    required_proofs_quote="Article 5 — Licence FFA ou PPS.",
)

# Ce que le navigateur envoie quand l'organisateur confirme l'écran sans rien changer.
CONFIRMED = {
    "transfer_allowed": "true",
    "transfer_allowed_quote": FOUND.transfer_allowed_quote,
    "transfer_deadline": "2027-03-28",
    "transfer_deadline_quote": FOUND.transfer_deadline_quote,
    "max_resale_price": "45,00",
    "max_resale_price_quote": FOUND.max_resale_price_quote,
    "transfer_fee": "",
    "transfer_fee_quote": "",
    "required_proofs": ["PPS", "LICENCE_FFA"],
    "required_proofs_quote": FOUND.required_proofs_quote,
}


@pytest.fixture
def reader(monkeypatch):
    """Remplace la lecture par l'IA : renvoie FOUND, ou lève l'erreur rangée dans reader["error"]."""
    state = {"error": None, "calls": []}

    def fake_read_rules(pdf):
        state["calls"].append(pdf)
        if state["error"]:
            raise state["error"]
        return FOUND

    monkeypatch.setattr(organizer, "read_rules", fake_read_rules)
    return state


def test_races_page_lists_races_and_their_status(client, session, race):
    assert "À renseigner" in client.get("/organizer").text

    session.add(RaceRulesRow(race_id=race.id, transfer_allowed=True))
    session.commit()

    page = client.get("/organizer").text
    assert race.name in page
    assert "Confirmées" in page


def test_rules_page_of_unknown_race_is_404(client):
    assert client.get("/organizer/races/999/rules").status_code == 404


def test_upload_shows_the_confirmation_screen_without_saving(client, session, race, reader):
    response = client.post(f"/organizer/races/{race.id}/rules/extract", files={"reglement": PDF})

    assert response.status_code == 200
    assert reader["calls"] == [PDF[1]]
    page = response.text
    assert 'value="2027-03-28"' in page
    assert 'value="45,00"' in page
    assert "Aucun transfert n&#39;est accepté après le 28 mars 2027." in page
    assert "Aucune mention dans le règlement." in page  # les frais, absents
    assert session.exec(select(RaceRulesRow)).first() is None


def test_upload_error_is_shown_on_the_upload_page(client, race, reader):
    reader["error"] = AIError("Le règlement doit être un fichier PDF.")

    response = client.post(f"/organizer/races/{race.id}/rules/extract", files={"reglement": PDF})

    assert response.status_code == 422
    assert "Le règlement doit être un fichier PDF." in response.text
    assert "Lire le règlement" in response.text


def test_upload_too_large_is_refused_before_calling_the_ai(client, race, reader, monkeypatch):
    monkeypatch.setattr(organizer, "MAX_PDF_BYTES", 10)

    response = client.post(f"/organizer/races/{race.id}/rules/extract", files={"reglement": PDF})

    assert response.status_code == 422
    assert reader["calls"] == []


def test_confirm_saves_the_rules_and_shows_them(client, session, race):
    response = client.post(f"/organizer/races/{race.id}/rules", data=CONFIRMED)

    assert response.status_code == 200
    assert response.history[0].status_code == 303
    assert "Les règles sont enregistrées." in response.text
    assert "45,00 €" in response.text
    row = session.exec(select(RaceRulesRow).where(RaceRulesRow.race_id == race.id)).one()
    assert row.transfer_allowed is True
    assert row.transfer_deadline == date(2027, 3, 28)
    assert row.max_resale_price_cents == 4500
    assert row.max_resale_price_quote == FOUND.max_resale_price_quote
    assert row.transfer_fee_cents is None
    assert row.required_proofs == ["PPS", "LICENCE_FFA"]


def test_confirm_again_replaces_the_rules(client, session, race):
    client.post(f"/organizer/races/{race.id}/rules", data=CONFIRMED)

    client.post(f"/organizer/races/{race.id}/rules", data={**CONFIRMED, "max_resale_price": "40"})

    row = session.exec(select(RaceRulesRow)).one()
    assert row.max_resale_price_cents == 4000


def test_confirm_drops_the_quote_of_a_rule_the_organizer_cleared(client, session, race):
    client.post(f"/organizer/races/{race.id}/rules", data={**CONFIRMED, "transfer_deadline": ""})

    row = session.exec(select(RaceRulesRow)).one()
    assert row.transfer_deadline is None
    assert row.transfer_deadline_quote is None


def test_confirm_keeps_a_rule_the_organizer_typed_without_quote(client, session, race):
    client.post(f"/organizer/races/{race.id}/rules", data={**CONFIRMED, "transfer_fee": "5"})

    row = session.exec(select(RaceRulesRow)).one()
    assert row.transfer_fee_cents == 500
    assert row.transfer_fee_quote is None


def test_confirm_with_unreadable_amount_saves_nothing(client, session, race):
    response = client.post(f"/organizer/races/{race.id}/rules", data={**CONFIRMED, "max_resale_price": "quarante"})

    assert response.status_code == 422
    assert "Montant illisible" in response.text
    assert 'value="quarante"' in response.text  # la saisie n'est pas perdue
    assert session.exec(select(RaceRulesRow)).first() is None


def test_forbidden_transfer_is_flagged(client, race):
    client.post(f"/organizer/races/{race.id}/rules", data={**CONFIRMED, "transfer_allowed": "false"})

    assert "interdit le transfert" in client.get(f"/organizer/races/{race.id}/rules").text


@pytest.mark.parametrize(
    "text, cents",
    [("45", 4500), ("45,5", 4550), ("45.50", 4550), (" 1 200,00 € ", 120000), ("0", 0), ("", None), ("  ", None)],
)
def test_parse_euros(text, cents):
    assert parse_euros(text) == cents


@pytest.mark.parametrize("text", ["quarante", "-5", "4,555", "NaN", "1e400"])
def test_parse_euros_refuses_bad_amounts(text):
    with pytest.raises(ValueError):
        parse_euros(text)
