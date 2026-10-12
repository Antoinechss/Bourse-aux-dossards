"""Tests du tableau de bord de l'organisateur : données de la table, indicateurs, valider et refuser."""

import pytest
from paypal_fakes import FakePayPal, reserved_transfer
from sqlmodel import select

from app.models import (
    LedgerKind,
    Listing,
    Notification,
    ProofType,
    Registration,
    Transfer,
    Verdict,
    Verification,
    Buyer,
)
from app.payments import ledger, service
from app.payments.client import PayPalError
from app.transfers import flow
from app.transfers.state import TransferState

DOUBT = "Le nom du justificatif (MARTIN Mari) ne correspond qu'en partie à Marie Martin."


@pytest.fixture
def paypal(monkeypatch):
    fake = FakePayPal()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def transfer(session, race, paypal):
    """Marie Martin a payé le dossard 42 (45 €) ; l'IA a un doute sur son justificatif : à l'organisateur de trancher."""
    session.add(Registration(race_id=race.id, bib_number=42, first_name="Jean", last_name="Dupont"))
    transfer = reserved_transfer(session, race)
    service.create_order(session, transfer)
    service.authorize(session, transfer)
    transfer.state = TransferState.AUTORISE
    session.add(transfer)
    session.add(
        Verification(
            transfer_id=transfer.id, proof_type=ProofType.LICENCE_FFA, verdict=Verdict.ORANGE, explanation=DOUBT
        )
    )
    session.commit()
    return transfer


def another_transfer(session, race, bib_number: int, state: TransferState) -> Transfer:
    listing = Listing(race_id=race.id, bib_number=bib_number, seller_email="autre@example.com", price_cents=2000)
    buyer = Buyer(race_id=race.id, first_name="Karim", last_name="Benali", email=f"karim{bib_number}@example.com")
    session.add_all([listing, buyer])
    session.commit()
    transfer = Transfer(listing_id=listing.id, buyer_id=buyer.id, state=state)
    session.add(transfer)
    session.commit()
    return transfer


def dashboard(client) -> dict:
    return client.get("/organizer/api/dashboard").json()


def subjects(session, email: str) -> list[str]:
    statement = select(Notification.subject).where(Notification.recipient_email == email).order_by(Notification.id)
    return list(session.exec(statement))


# --- La page ---


def test_dashboard_page_loads_ag_grid_pinned_and_keeps_the_races(client, race):
    page = client.get("/organizer").text

    assert 'id="transfers-grid"' in page
    assert "https://cdn.jsdelivr.net/npm/ag-grid-community@35.3.1/dist/ag-grid-community.min.js" in page
    assert "/static/dashboard.js" in page
    assert f"/organizer/races/{race.id}/rules" in page
    assert client.get("/static/dashboard.js").status_code == 200


# --- Les données de la table ---


def test_dashboard_is_empty_before_any_listing(client):
    assert dashboard(client) == {
        "indicators": {"reassigned": 0, "listed": 0, "saved_percent": None, "organizer_commission_cents": 0},
        "transfers": [],
    }


def test_a_transfer_waiting_for_the_organizer_can_be_approved_or_refused(client, transfer):
    [row] = dashboard(client)["transfers"]

    assert row == {
        "id": transfer.id,
        "race": "Trail des Crêtes",
        "bib_number": 42,
        "state": "AUTORISE",
        "state_label": "Autorisé",
        "buyer": "Marie Martin",
        "price_cents": 4500,
        "organizer_fee_cents": 135,  # 3 % de 45 €
        "total_cents": 4860,  # 45 € + 8 % de frais de service
        "verdict": "ORANGE",
        "explanation": DOUBT,
        "decision": "À décider",
        "can_approve": True,
        "can_refuse": True,
    }


def test_rows_say_who_decided_and_come_newest_first(client, session, race, transfer):
    on_sale = Transfer(listing_id=transfer.listing_id)  # personne à qui le proposer
    waiting_for_proof = another_transfer(session, race, 50, TransferState.AUTORISE)
    automatic = another_transfer(session, race, 51, TransferState.VERIFIE)
    session.add(on_sale)
    session.add(
        Verification(transfer_id=automatic.id, proof_type=ProofType.PPS, verdict=Verdict.VERT, explanation="Tout concorde.")
    )
    session.commit()

    rows = {row["id"]: row for row in dashboard(client)["transfers"]}

    assert list(rows) == sorted(rows, reverse=True)
    assert (rows[on_sale.id]["buyer"], rows[on_sale.id]["verdict"], rows[on_sale.id]["decision"]) == (None, None, "")
    # Payé mais sans justificatif : rien à valider, mais l'organisateur peut refuser pour libérer les fonds.
    assert rows[waiting_for_proof.id]["decision"] == "Justificatif attendu"
    assert (rows[waiting_for_proof.id]["can_approve"], rows[waiting_for_proof.id]["can_refuse"]) == (False, True)
    assert rows[automatic.id]["decision"] == "Validation automatique"
    assert (rows[automatic.id]["can_approve"], rows[automatic.id]["can_refuse"]) == (False, False)


# --- Les indicateurs ---


def test_indicators_count_reassigned_bibs_and_the_commission_actually_paid(client, session, race, transfer):
    another_transfer(session, race, 50, TransferState.EN_VENTE)
    another_transfer(session, race, 51, TransferState.NOM_CHANGE)
    paid = another_transfer(session, race, 52, TransferState.VERSE)
    sent = another_transfer(session, race, 53, TransferState.ENCAISSE)
    ledger.record(session, paid.id, LedgerKind.VERSEMENT_ORGANISATEUR, 60, "BATCH-A")
    ledger.record(session, paid.id, LedgerKind.VERSEMENT_VENDEUR, 2000, "BATCH-A")
    # Versement parti mais pas encore confirmé par PayPal : pas encore « reversé ».
    ledger.record(session, sent.id, LedgerKind.VERSEMENT_ORGANISATEUR, 60, "BATCH-B")
    session.commit()

    assert dashboard(client)["indicators"] == {
        "reassigned": 3,  # nom changé, encaissé, versé
        "listed": 5,
        "saved_percent": 60,
        "organizer_commission_cents": 60,
    }


# --- Valider ---


def test_approving_verifies_the_transfer_and_tells_the_buyer(client, session, transfer):
    response = client.post(f"/organizer/api/transfers/{transfer.id}/approve")

    assert response.json() == {"state": "VERIFIE"}
    assert session.get(Transfer, transfer.id).state is TransferState.VERIFIE
    assert session.exec(select(Verification)).one().organizer_approved is True
    assert subjects(session, "marie@example.com") == ["Transfert validé par l'organisateur — Trail des Crêtes"]
    [row] = dashboard(client)["transfers"]
    assert (row["decision"], row["can_approve"], row["can_refuse"]) == ("Validé par l'organisateur", False, False)


def test_an_approved_transfer_then_runs_to_the_end_like_a_green_one(client, session, transfer):
    client.post(f"/organizer/api/transfers/{transfer.id}/approve")

    flow.housekeeping(session)

    assert session.get(Transfer, transfer.id).state is TransferState.VERSE
    assert dashboard(client)["indicators"] == {
        "reassigned": 1,
        "listed": 1,
        "saved_percent": 100,
        "organizer_commission_cents": 135,
    }


def test_approving_twice_is_refused(client, session, transfer):
    client.post(f"/organizer/api/transfers/{transfer.id}/approve")

    response = client.post(f"/organizer/api/transfers/{transfer.id}/approve")

    assert response.status_code == 409
    assert response.json()["detail"] == "Ce transfert n'attend pas de validation."
    assert len(subjects(session, "marie@example.com")) == 1


def test_a_transfer_without_proof_cannot_be_approved(client, session, race):
    waiting_for_proof = another_transfer(session, race, 50, TransferState.AUTORISE)

    assert client.post(f"/organizer/api/transfers/{waiting_for_proof.id}/approve").status_code == 409
    assert session.get(Transfer, waiting_for_proof.id).state is TransferState.AUTORISE


# --- Refuser ---


def test_refusing_voids_the_authorization_and_tells_both_sides(client, session, transfer, paypal):
    response = client.post(f"/organizer/api/transfers/{transfer.id}/refuse")

    assert response.json() == {"state": "REFUSE"}
    assert session.get(Transfer, transfer.id).state is TransferState.REFUSE
    assert paypal.operations()[-1] == "void_authorization"
    # Le journal garde la trace des fonds bloqués puis libérés ; rien n'a été encaissé.
    assert [entry.kind for entry in ledger.entries(session, transfer.id)] == [
        LedgerKind.AUTORISATION,
        LedgerKind.ANNULATION,
    ]
    assert session.exec(select(Verification)).one().organizer_approved is False
    assert subjects(session, "marie@example.com") == ["Transfert refusé : vous n'êtes pas débité — Trail des Crêtes"]
    assert subjects(session, "vendeur@example.com") == ["Transfert refusé — dossard n°42"]
    assert dashboard(client)["transfers"][0]["decision"] == "Refusé par l'organisateur"


def test_refusal_does_not_happen_if_paypal_cannot_void(client, session, transfer, paypal):
    paypal.error = PayPalError("panne simulée")

    response = client.post(f"/organizer/api/transfers/{transfer.id}/refuse")

    assert response.status_code == 502
    assert "le transfert n'est pas refusé" in response.json()["detail"]
    assert session.get(Transfer, transfer.id).state is TransferState.AUTORISE
    assert session.exec(select(Verification)).one().organizer_approved is None
    assert subjects(session, "marie@example.com") == []

    paypal.error = None

    assert client.post(f"/organizer/api/transfers/{transfer.id}/refuse").json() == {"state": "REFUSE"}


@pytest.mark.parametrize("state", [TransferState.RESERVE, TransferState.VERIFIE, TransferState.VERSE])
def test_only_an_authorized_transfer_can_be_refused(client, session, transfer, paypal, state):
    transfer.state = state
    session.add(transfer)
    session.commit()
    calls = len(paypal.calls)

    response = client.post(f"/organizer/api/transfers/{transfer.id}/refuse")

    assert response.status_code == 409
    assert session.get(Transfer, transfer.id).state is state
    assert len(paypal.calls) == calls


def test_decisions_on_an_unknown_transfer_are_404(client):
    assert client.post("/organizer/api/transfers/999/approve").status_code == 404
    assert client.post("/organizer/api/transfers/999/refuse").status_code == 404
