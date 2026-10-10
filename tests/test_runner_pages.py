"""Tests des pages coureurs : course, liste d'attente, mise en vente, suivi, justificatif."""

from datetime import date

import pytest
from paypal_fakes import FakePayPal, reserved_transfer
from sqlmodel import select

from app.ai.client import AIError
from app.ai.verifier import DocumentType, ProofReading
from app.models import Listing, Notification, RaceRulesRow, Registration, Transfer, Verification, WaitlistEntry
from app.transfers import verification
from app.transfers.state import TransferState

LICENCE = ProofReading(
    document_type=DocumentType.LICENCE_FFA, holder_name="MARTIN Marie", valid_until=date(2027, 8, 31), concern=None
)
PROOF = {"justificatif": ("licence.png", b"\x89PNG\r\n\x1a\n une image", "image/png")}
SELLER = {"bib_number": "42", "first_name": "Jean", "last_name": "Dupont", "email": "Jean@Example.com", "price": "45"}
BUYER = {"first_name": "Marie", "last_name": "Martin", "email": "marie@example.com"}


@pytest.fixture
def rules(session, race):
    """Revente autorisée, plafonnée à 45 € ; le dossard 42 est celui de Jean Dupont."""
    rules = RaceRulesRow(race_id=race.id, transfer_allowed=True, max_resale_price_cents=4500)
    session.add_all([rules, Registration(race_id=race.id, bib_number=42, first_name="Jean", last_name="Dupont")])
    session.commit()
    return rules


@pytest.fixture
def paypal(monkeypatch):
    fake = FakePayPal()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def transfer(session, race):
    """Le dossard 42 est proposé à Marie Martin, qui n'a pas encore payé."""
    return reserved_transfer(session, race)


@pytest.fixture
def reader(monkeypatch):
    """Remplace la lecture du justificatif par l'IA : renvoie reader["reading"], ou lève reader["error"]."""
    state = {"reading": LICENCE, "error": None}

    def fake_read_proof(data, client=None):
        if state["error"]:
            raise state["error"]
        return state["reading"]

    monkeypatch.setattr(verification, "read_proof", fake_read_proof)
    return state


def set_state(session, transfer, state: TransferState) -> None:
    transfer.state = state
    session.add(transfer)
    session.commit()


# --- La course et la liste d'attente ---


def test_race_page_shows_what_is_on_sale_and_the_rules(client, race, rules):
    page = client.get(f"/runner/races/{race.id}").text
    assert "Aucun dossard n'est en vente pour l'instant." in page
    assert "Prix de revente maximum : 45,00 €" in page

    client.post(f"/runner/races/{race.id}/listings", data=SELLER)

    assert "1 dossard en vente." in client.get(f"/runner/races/{race.id}").text


def test_race_page_is_closed_when_the_rules_forbid_resale(client, session, race, rules):
    rules.transfer_allowed = False
    session.add(rules)
    session.commit()

    page = client.get(f"/runner/races/{race.id}").text

    assert "interdit la revente" in page
    assert "hx-post" not in page  # ni liste d'attente, ni mise en vente


def test_unknown_race_is_404(client):
    assert client.get("/runner/races/999").status_code == 404
    assert client.get("/runner/races/999/sell").status_code == 404


def test_joining_the_waitlist_answers_with_the_position(client, session, race, rules):
    response = client.post(f"/runner/races/{race.id}/waitlist", data=BUYER)

    assert response.status_code == 200
    assert "vous êtes n°1 sur la liste d'attente" in response.text
    assert session.exec(select(WaitlistEntry)).one().email == "marie@example.com"


@pytest.mark.parametrize(
    "changes, expected",
    [({"email": "pas-un-email"}, "Adresse email invalide."), ({"first_name": "  "}, "Prénom manquant.")],
)
def test_joining_with_a_bad_entry_shows_the_error(client, session, race, rules, changes, expected):
    response = client.post(f"/runner/races/{race.id}/waitlist", data={**BUYER, **changes})

    assert response.status_code == 422
    assert expected in response.text
    assert session.exec(select(WaitlistEntry)).first() is None


def test_joining_twice_shows_the_error(client, race, rules):
    client.post(f"/runner/races/{race.id}/waitlist", data=BUYER)

    response = client.post(f"/runner/races/{race.id}/waitlist", data=BUYER)

    assert response.status_code == 422
    assert "déjà inscrite" in response.text


def test_joining_when_a_bib_is_waiting_links_to_the_offer(client, session, race, rules):
    client.post(f"/runner/races/{race.id}/listings", data=SELLER)

    response = client.post(f"/runner/races/{race.id}/waitlist", data=BUYER)

    transfer = session.exec(select(Transfer)).one()
    assert f'href="/runner/transfers/{transfer.id}"' in response.text


# --- La mise en vente ---


def test_sell_page_shows_the_price_cap(client, race, rules):
    assert "45,00 € au plus, d'après le règlement" in client.get(f"/runner/races/{race.id}/sell").text


def test_price_check_accepts_a_price_under_the_cap_and_shows_what_each_side_gets(client, race, rules):
    response = client.get(f"/runner/races/{race.id}/price-check", params={"price": "45"})

    assert response.status_code == 200
    assert "Vous recevrez 45,00 €" in response.text
    assert "paiera 48,60 €, frais de service de 3,60 € compris" in response.text


@pytest.mark.parametrize(
    "price, expected", [("45,01", "plafonne la revente à 45,00 €"), ("abc", "Montant illisible"), ("0", "supérieur à zéro")]
)
def test_price_check_refuses_a_bad_price(client, race, rules, price, expected):
    response = client.get(f"/runner/races/{race.id}/price-check", params={"price": price})

    assert response.status_code == 422
    assert expected in response.text


def test_price_check_says_nothing_while_the_field_is_empty(client, race, rules):
    response = client.get(f"/runner/races/{race.id}/price-check", params={"price": ""})

    assert (response.status_code, response.text) == (200, "")


def test_listing_a_bib_opens_its_tracking_page(client, session, race, rules):
    response = client.post(f"/runner/races/{race.id}/listings", data=SELLER)

    listing = session.exec(select(Listing)).one()
    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == f"/runner/listings/{listing.id}"
    assert (listing.price_cents, listing.seller_email) == (4500, "jean@example.com")
    page = client.get(f"/runner/listings/{listing.id}").text
    assert "Suivi de votre vente" in page
    assert "Votre dossard est en vente à 45,00 €" in page


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"price": "46"}, "plafonne la revente à 45,00 €"),
        ({"price": ""}, "Indiquez le prix demandé."),
        ({"bib_number": "quarante-deux"}, "Numéro de dossard invalide."),
        ({"last_name": "Durand"}, "n&#39;est pas inscrit à ce nom"),
        ({"email": "jean"}, "Adresse email invalide."),
    ],
)
def test_listing_with_a_bad_entry_shows_the_error_and_saves_nothing(client, session, race, rules, changes, expected):
    response = client.post(f"/runner/races/{race.id}/listings", data={**SELLER, **changes})

    assert response.status_code == 422
    assert expected in response.text
    assert session.exec(select(Listing)).first() is None


# --- Le suivi ---


def test_buyer_tracking_of_an_offer_links_to_the_payment(client, transfer):
    page = client.get(f"/runner/transfers/{transfer.id}").text

    assert "Suivi de votre achat" in page
    assert "Bonjour Marie, ce dossard vous est proposé à 45,00 €" in page
    assert f'href="/runner/transfers/{transfer.id}/checkout"' in page
    assert f'hx-get="/runner/transfers/{transfer.id}/status?seen={transfer.id}-RESERVE"' in page


def test_tracking_reloads_the_page_only_when_the_state_has_changed(client, session, transfer):
    url = f"/runner/transfers/{transfer.id}/status"

    unchanged = client.get(url, params={"seen": f"{transfer.id}-RESERVE"})
    assert unchanged.status_code == 204
    assert "HX-Refresh" not in unchanged.headers

    set_state(session, transfer, TransferState.AUTORISE)

    assert client.get(url, params={"seen": f"{transfer.id}-RESERVE"}).headers["HX-Refresh"] == "true"


def test_seller_tracking_follows_the_bib_from_one_buyer_to_the_next(client, session, transfer):
    url = f"/runner/listings/{transfer.listing_id}/status"
    set_state(session, transfer, TransferState.OFFRE_EXPIREE)
    session.add(Transfer(listing_id=transfer.listing_id))  # le dossard repart en vente
    session.commit()

    assert client.get(url, params={"seen": f"{transfer.id}-RESERVE"}).headers["HX-Refresh"] == "true"
    assert "Votre dossard est en vente" in client.get(f"/runner/listings/{transfer.listing_id}").text


@pytest.mark.parametrize(
    "state, buyer_sees, seller_sees",
    [
        (TransferState.VERIFIE, "Justificatif validé.", "L'acheteur est vérifié."),
        (TransferState.NOM_CHANGE, "Le dossard est à votre nom.", "Le dossard est au nom de l'acheteur."),
        (TransferState.ENCAISSE, "Paiement encaissé", "Votre versement de 45,00 € est en route"),
        (TransferState.VERSE, "Paiement encaissé", "Vous avez été payé : 45,00 €"),
        (TransferState.REFUSE, "vous n'avez pas été débité", "Remettre mon dossard en vente"),
    ],
)
def test_tracking_tells_each_side_where_the_transfer_stands(client, session, transfer, state, buyer_sees, seller_sees):
    set_state(session, transfer, state)

    assert buyer_sees in client.get(f"/runner/transfers/{transfer.id}").text
    assert seller_sees in client.get(f"/runner/listings/{transfer.listing_id}").text


def test_tracking_stops_asking_once_the_transfer_is_over(client, session, transfer):
    set_state(session, transfer, TransferState.VERSE)

    assert "hx-get" not in client.get(f"/runner/transfers/{transfer.id}").text
    assert "hx-get" not in client.get(f"/runner/listings/{transfer.listing_id}").text


def test_unknown_tracking_pages_are_404(client):
    assert client.get("/runner/transfers/999").status_code == 404
    assert client.get("/runner/listings/999").status_code == 404


# --- Le paiement et le justificatif ---


def test_authorizing_the_payment_notifies_buyer_and_seller(client, session, transfer, paypal):
    client.post(f"/runner/transfers/{transfer.id}/order")
    client.post(f"/runner/transfers/{transfer.id}/authorize")

    messages = {n.recipient_email: n for n in session.exec(select(Notification))}
    assert "déposez votre justificatif" in messages["marie@example.com"].subject
    assert messages["marie@example.com"].link == f"/runner/transfers/{transfer.id}"
    assert "Un acheteur a payé votre dossard n°42" in messages["vendeur@example.com"].subject


def test_authorized_buyer_is_asked_for_a_proof(client, session, transfer):
    set_state(session, transfer, TransferState.AUTORISE)

    page = client.get(f"/runner/transfers/{transfer.id}").text

    assert "déposez votre licence FFA ou votre PPS" in page
    assert f'hx-post="/runner/transfers/{transfer.id}/proof"' in page


def test_green_proof_verifies_the_transfer_and_reloads_the_page(client, session, transfer, reader):
    set_state(session, transfer, TransferState.AUTORISE)

    response = client.post(f"/runner/transfers/{transfer.id}/proof", files=PROOF)

    assert response.status_code == 204
    assert response.headers["HX-Refresh"] == "true"
    assert session.get(Transfer, transfer.id).state is TransferState.VERIFIE
    assert "Justificatif validé." in client.get(f"/runner/transfers/{transfer.id}").text


def test_doubtful_proof_is_explained_and_another_one_can_be_sent(client, session, transfer, reader):
    set_state(session, transfer, TransferState.AUTORISE)
    reader["reading"] = LICENCE.model_copy(update={"holder_name": "Karim BENALI"})

    client.post(f"/runner/transfers/{transfer.id}/proof", files=PROOF)

    assert session.get(Transfer, transfer.id).state is TransferState.AUTORISE
    page = client.get(f"/runner/transfers/{transfer.id}").text
    assert "Le justificatif est au nom de Karim BENALI, et non de Marie Martin." in page
    assert "L'organisateur va trancher" in page
    assert f'hx-post="/runner/transfers/{transfer.id}/proof"' in page
    # Le vendeur, lui, ne voit pas ce que contient le justificatif de l'acheteur.
    assert "Karim" not in client.get(f"/runner/listings/{transfer.listing_id}").text


def test_failed_analysis_shows_the_error_and_saves_nothing(client, session, transfer, reader):
    set_state(session, transfer, TransferState.AUTORISE)
    reader["error"] = AIError("Le service d'analyse est injoignable : vérifie la connexion et réessaie.")

    response = client.post(f"/runner/transfers/{transfer.id}/proof", files=PROOF)

    assert response.status_code == 422
    assert "injoignable" in response.text
    assert session.exec(select(Verification)).first() is None


def test_proof_is_refused_when_the_transfer_does_not_expect_one(client, transfer, reader):
    response = client.post(f"/runner/transfers/{transfer.id}/proof", files=PROOF)  # pas encore payé

    assert response.status_code == 422
    assert "n&#39;attend pas de justificatif" in response.text
