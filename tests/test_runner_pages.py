"""Tests des pages coureurs : course, mise en vente, achat (réservation, contrôles), suivi, justificatif."""

from datetime import date, timedelta

import pytest
from paypal_fakes import FakePayPal, reserved_transfer
from sqlmodel import select

from app.ai.client import AIError
from app.ai.verifier import DocumentType, ProofReading
from app.models import Buyer, Listing, Notification, RaceRulesRow, Registration, Transfer, Verification, WaitlistEntry, utcnow
from app.transfers import verification
from app.transfers.state import TransferState

LICENCE = ProofReading(
    document_type=DocumentType.LICENCE_FFA, holder_name="MARTIN Marie", valid_until=date(2027, 8, 31), concern=None
)
PROOF = {"justificatif": ("licence.png", b"\x89PNG\r\n\x1a\n une image", "image/png")}
SELLER = {"bib_number": "42", "first_name": "Jean", "last_name": "Dupont", "email": "Jean@Example.com", "price": "45"}
WAITING = {"first_name": "Karim", "last_name": "Benali", "email": "karim@example.com"}
IDENTITY = {"first_name": "Marie", "last_name": "Martin", "email": "Marie@Example.com", "birth_date": "1990-05-02"}


@pytest.fixture
def rules(session, race):
    """Revente autorisée, plafonnée à 45 € ; il faut 18 ans ; le dossard 42 est celui de Jean Dupont."""
    race.minimum_age = 18
    rules = RaceRulesRow(race_id=race.id, transfer_allowed=True, max_resale_price_cents=4500)
    session.add_all([race, rules, Registration(race_id=race.id, bib_number=42, first_name="Jean", last_name="Dupont")])
    session.commit()
    return rules


@pytest.fixture
def paypal(monkeypatch):
    fake = FakePayPal()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def on_sale(client, session, race, rules):
    """Le dossard 42 de Jean Dupont, vérifié et en vente à 45 €."""
    client.post(f"/runner/races/{race.id}/listings", data=SELLER)
    return session.exec(select(Transfer)).one()


@pytest.fixture
def transfer(session, race):
    """Le dossard 42 est réservé par Marie Martin, dont les contrôles sont passés : elle n'a pas encore payé."""
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


def states(session) -> list[TransferState]:
    return [transfer.state for transfer in session.exec(select(Transfer).order_by(Transfer.id))]


# --- La course ---


def test_race_page_shows_the_bibs_on_sale_and_the_rules(client, race, rules):
    page = client.get(f"/runner/races/{race.id}").text
    assert "Aucun dossard n'est en vente pour l'instant." in page
    assert "<dt>Prix de revente maximum</dt><dd>45,00 €</dd>" in page

    client.post(f"/runner/races/{race.id}/listings", data=SELLER)

    page = client.get(f"/runner/races/{race.id}").text
    assert "Aucun dossard n'est en vente" not in page
    assert '<span class="figure__label">dossard en vente</span>' in page
    assert '<div class="bib__number">42</div>' in page
    assert '<span class="bib-card__price">45,00 €</span>' in page
    assert "Vérifié par l'organisateur" in page
    assert "Dupont" not in page  # le nom du vendeur n'est jamais affiché


def test_a_bib_on_sale_has_a_buy_button_and_a_reserved_one_has_none(client, session, race, on_sale):
    assert f'action="/runner/transfers/{on_sale.id}/reserve"' in client.get(f"/runner/races/{race.id}").text

    client.post(f"/runner/transfers/{on_sale.id}/reserve")

    page = client.get(f"/runner/races/{race.id}").text
    assert "Réservé par un coureur" in page
    assert "/reserve" not in page


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


# --- La liste d'attente ---


def test_joining_the_waitlist_promises_an_email(client, session, race, rules):
    response = client.post(f"/runner/races/{race.id}/waitlist", data=WAITING)

    assert response.status_code == 200
    assert "nous écrirons à karim@example.com dès qu'un dossard sera mis en vente" in response.text
    assert session.exec(select(WaitlistEntry)).one().email == "karim@example.com"


@pytest.mark.parametrize(
    "changes, expected",
    [({"email": "pas-un-email"}, "Adresse email invalide."), ({"first_name": "  "}, "Prénom manquant.")],
)
def test_joining_with_a_bad_entry_shows_the_error(client, session, race, rules, changes, expected):
    response = client.post(f"/runner/races/{race.id}/waitlist", data={**WAITING, **changes})

    assert response.status_code == 422
    assert expected in response.text
    assert session.exec(select(WaitlistEntry)).first() is None


def test_joining_twice_shows_the_error(client, race, rules):
    client.post(f"/runner/races/{race.id}/waitlist", data=WAITING)

    response = client.post(f"/runner/races/{race.id}/waitlist", data=WAITING)

    assert response.status_code == 422
    assert "déjà inscrite" in response.text


# --- La mise en vente ---


def test_sell_page_lists_the_registered_runners_of_the_demo_registry(client, race, rules):
    page = client.get(f"/runner/races/{race.id}/sell").text

    assert "<td>42</td><td>Jean</td><td>Dupont</td>" in page


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
    assert "Votre dossard est vérifié et en vente à 45,00 €" in page


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"price": "46"}, "plafonne la revente à 45,00 €"),
        ({"price": ""}, "Indiquez le prix demandé."),
        ({"bib_number": "quarante-deux"}, "Numéro de dossard invalide."),
        ({"bib_number": "101"}, "Aucun dossard n°101 parmi les inscrits de « Trail des Crêtes »."),
        ({"last_name": "Durand"}, "n&#39;est pas inscrit à ce nom"),
        ({"email": "jean"}, "Adresse email invalide."),
    ],
)
def test_listing_with_a_bad_entry_shows_the_error_and_saves_nothing(client, session, race, rules, changes, expected):
    response = client.post(f"/runner/races/{race.id}/listings", data={**SELLER, **changes})

    assert response.status_code == 422
    assert expected in response.text
    assert session.exec(select(Listing)).first() is None


# --- L'achat : réserver, puis donner son identité ---


def test_buying_reserves_the_bib_and_asks_for_the_buyer_s_identity(client, session, on_sale):
    response = client.post(f"/runner/transfers/{on_sale.id}/reserve")

    assert response.history[0].status_code == 303
    assert response.url.path == f"/runner/transfers/{on_sale.id}/buyer"
    assert states(session) == [TransferState.RESERVE]
    assert "Ce dossard vous est réservé jusqu'à" in response.text
    assert "18 ans le jour de la course" in response.text
    assert "Rien n'est débité à cette étape." in response.text


def test_a_bib_already_reserved_cannot_be_bought(client, session, race, on_sale):
    client.post(f"/runner/transfers/{on_sale.id}/reserve")

    response = client.post(f"/runner/transfers/{on_sale.id}/reserve")

    assert response.status_code == 409
    assert "vient d&#39;être réservé par un autre coureur" in response.text


def test_an_eligible_buyer_is_sent_to_the_payment(client, session, on_sale):
    client.post(f"/runner/transfers/{on_sale.id}/reserve")

    response = client.post(f"/runner/transfers/{on_sale.id}/buyer", data=IDENTITY)

    assert response.url.path == f"/runner/transfers/{on_sale.id}/checkout"
    assert states(session) == [TransferState.ELIGIBLE]
    buyer = session.exec(select(Buyer)).one()
    assert (buyer.first_name, buyer.email, buyer.birth_date) == ("Marie", "marie@example.com", date(1990, 5, 2))
    assert "<paypal-button" in response.text


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"birth_date": "2012-06-01"}, "Il faut avoir 18 ans le jour de la course pour y participer."),
        ({"first_name": "Jean", "last_name": "Dupont"}, "Vous êtes déjà inscrit à cette course"),
    ],
)
def test_an_ineligible_buyer_is_told_why_and_the_bib_goes_back_on_sale(client, session, race, on_sale, changes, expected):
    client.post(f"/runner/transfers/{on_sale.id}/reserve")

    response = client.post(f"/runner/transfers/{on_sale.id}/buyer", data={**IDENTITY, **changes})

    assert "Vous ne pouvez pas reprendre ce dossard" in response.text
    assert expected in response.text
    assert "aucun paiement n&#39;a été déclenché" in response.text
    assert states(session) == [TransferState.INELIGIBLE, TransferState.EN_VENTE]
    assert "<paypal-button" not in response.text
    # Le dossard est de nouveau achetable sur la page de la course.
    assert "/reserve" in client.get(f"/runner/races/{race.id}").text


@pytest.mark.parametrize(
    "changes, expected",
    [({"birth_date": ""}, "Indiquez votre date de naissance."), ({"email": "marie"}, "Adresse email invalide.")],
)
def test_an_incomplete_identity_can_be_corrected_without_losing_the_reservation(client, session, on_sale, changes, expected):
    client.post(f"/runner/transfers/{on_sale.id}/reserve")

    response = client.post(f"/runner/transfers/{on_sale.id}/buyer", data={**IDENTITY, **changes})

    assert response.status_code == 422
    assert expected in response.text
    assert 'value="Martin"' in response.text  # la saisie n'est pas perdue
    assert states(session) == [TransferState.RESERVE]


def test_the_identity_page_of_an_expired_reservation_says_so(client, session, on_sale):
    client.post(f"/runner/transfers/{on_sale.id}/reserve")
    on_sale.offer_expires_at = utcnow() - timedelta(minutes=1)
    session.add(on_sale)
    session.commit()

    assert "Cette réservation a expiré" in client.get(f"/runner/transfers/{on_sale.id}/buyer").text
    response = client.post(f"/runner/transfers/{on_sale.id}/buyer", data=IDENTITY)
    assert "Cette réservation a expiré" in response.text
    assert session.exec(select(Buyer)).first() is None


def test_unknown_bib_cannot_be_bought(client):
    assert client.post("/runner/transfers/999/reserve").status_code == 404
    assert client.get("/runner/transfers/999/buyer").status_code == 404


# --- Le suivi ---


def test_buyer_tracking_of_an_eligible_buyer_links_to_the_payment(client, transfer):
    page = client.get(f"/runner/transfers/{transfer.id}").text

    assert "Suivi de votre achat" in page
    assert "Bonjour Marie, vos contrôles sont passés." in page
    assert f'href="/runner/transfers/{transfer.id}/checkout"' in page
    assert f'hx-get="/runner/transfers/{transfer.id}/status?seen={transfer.id}-ELIGIBLE"' in page


def test_tracking_lists_the_eight_steps_in_order(client, transfer):
    page = client.get(f"/runner/transfers/{transfer.id}").text
    labels = [
        "Dossard vérifié",
        "En vente",
        "Réservé",
        "Acheteur éligible",
        "Paiement autorisé",
        "Dossard au nom de l&#39;acheteur",
        "Paiement encaissé",
        "Vendeur payé, commission versée",
    ]

    positions = [page.index(f">{label}</li>") for label in labels]

    assert positions == sorted(positions)
    assert '<li class=" current">Acheteur éligible</li>' in page


def test_tracking_reloads_the_page_only_when_the_state_has_changed(client, session, transfer):
    url = f"/runner/transfers/{transfer.id}/status"

    unchanged = client.get(url, params={"seen": f"{transfer.id}-ELIGIBLE"})
    assert unchanged.status_code == 204
    assert "HX-Refresh" not in unchanged.headers

    set_state(session, transfer, TransferState.AUTORISE)

    assert client.get(url, params={"seen": f"{transfer.id}-ELIGIBLE"}).headers["HX-Refresh"] == "true"


def test_seller_tracking_follows_the_bib_from_one_buyer_to_the_next(client, session, transfer):
    url = f"/runner/listings/{transfer.listing_id}/status"
    set_state(session, transfer, TransferState.RESERVATION_EXPIREE)
    session.add(Transfer(listing_id=transfer.listing_id, state=TransferState.EN_VENTE))  # le dossard repart en vente
    session.commit()

    assert client.get(url, params={"seen": f"{transfer.id}-ELIGIBLE"}).headers["HX-Refresh"] == "true"
    assert "Votre dossard est vérifié et en vente" in client.get(f"/runner/listings/{transfer.listing_id}").text


@pytest.mark.parametrize(
    "state, buyer_sees, seller_sees",
    [
        (TransferState.AUTORISE, "L'organisateur confirme le transfert.", "Un acheteur a payé : la somme est bloquée."),
        (TransferState.NOM_CHANGE, "Le dossard est à votre nom.", "Le dossard est au nom de l'acheteur."),
        (TransferState.ENCAISSE, "Paiement encaissé", "Votre versement de 45,00 € est en route"),
        (TransferState.VERSE, "Paiement encaissé", "Vous avez été payé : 45,00 €"),
        (TransferState.REFUSE, "vous n'avez pas été débité", "Remettre mon dossard en vente"),
        (TransferState.RESERVATION_EXPIREE, "Votre réservation a expiré", "votre dossard repart en vente"),
        (TransferState.INELIGIBLE, "aucun paiement n'a été déclenché", "votre dossard repart en vente"),
    ],
)
def test_tracking_tells_each_side_where_the_transfer_stands(client, session, transfer, state, buyer_sees, seller_sees):
    set_state(session, transfer, state)

    assert buyer_sees in client.get(f"/runner/transfers/{transfer.id}").text
    assert seller_sees in client.get(f"/runner/listings/{transfer.listing_id}").text


def test_the_bib_carries_the_buyer_s_name_only_for_the_buyer(client, session, transfer):
    set_state(session, transfer, TransferState.NOM_CHANGE)

    assert '<span class="bib__name-text">Marie Martin</span>' in client.get(f"/runner/transfers/{transfer.id}").text
    seller_page = client.get(f"/runner/listings/{transfer.listing_id}").text
    assert '<span class="bib__name-text">Transféré</span>' in seller_page
    assert "Martin" not in seller_page


def test_tracking_stops_asking_once_the_transfer_is_over(client, session, transfer):
    set_state(session, transfer, TransferState.VERSE)

    assert "hx-get" not in client.get(f"/runner/transfers/{transfer.id}").text
    assert "hx-get" not in client.get(f"/runner/listings/{transfer.listing_id}").text


def test_unknown_tracking_pages_are_404(client):
    assert client.get("/runner/transfers/999").status_code == 404
    assert client.get("/runner/listings/999").status_code == 404


# --- Le paiement ---


def test_authorizing_the_payment_notifies_buyer_and_seller(client, session, transfer, paypal):
    client.post(f"/runner/transfers/{transfer.id}/order")
    client.post(f"/runner/transfers/{transfer.id}/authorize")

    messages = {n.recipient_email: n for n in session.exec(select(Notification))}
    assert "Paiement autorisé" in messages["marie@example.com"].subject
    assert messages["marie@example.com"].link == f"/runner/transfers/{transfer.id}"
    assert "Un acheteur a payé votre dossard n°42" in messages["vendeur@example.com"].subject


# --- Le justificatif : après l'achat, sans le bloquer ---


def test_no_proof_is_asked_before_the_bib_is_paid(client, transfer, reader):
    assert "justificatif" not in client.get(f"/runner/transfers/{transfer.id}").text.lower()

    response = client.post(f"/runner/transfers/{transfer.id}/proof", files=PROOF)

    assert response.status_code == 422
    assert "une fois le dossard payé" in response.text


def test_a_paid_buyer_is_told_the_proof_is_due_before_bib_pickup(client, session, transfer):
    set_state(session, transfer, TransferState.ENCAISSE)

    page = client.get(f"/runner/transfers/{transfer.id}").text

    assert "À fournir avant le retrait du dossard" in page
    assert "Votre achat n'en dépend pas." in page
    assert f'hx-post="/runner/transfers/{transfer.id}/proof"' in page


def test_a_green_proof_is_marked_supplied_and_changes_nothing_else(client, session, transfer, reader):
    set_state(session, transfer, TransferState.AUTORISE)

    response = client.post(f"/runner/transfers/{transfer.id}/proof", files=PROOF)

    assert response.status_code == 204
    assert response.headers["HX-Refresh"] == "true"
    page = client.get(f"/runner/transfers/{transfer.id}").text
    assert "Justificatif fourni." in page
    assert "hx-post" not in page  # plus rien à déposer
    assert session.get(Transfer, transfer.id).state is TransferState.AUTORISE


def test_a_doubtful_proof_is_explained_and_another_one_can_be_sent(client, session, transfer, reader):
    set_state(session, transfer, TransferState.NOM_CHANGE)
    reader["reading"] = LICENCE.model_copy(update={"holder_name": "Karim BENALI"})

    client.post(f"/runner/transfers/{transfer.id}/proof", files=PROOF)

    page = client.get(f"/runner/transfers/{transfer.id}").text
    assert "Le justificatif est au nom de Karim BENALI, et non de Marie Martin." in page
    assert "L'organisateur va trancher" in page
    assert f'hx-post="/runner/transfers/{transfer.id}/proof"' in page
    assert "Le dossard est à votre nom." in page  # l'achat suit son cours
    # Le vendeur, lui, ne voit rien du justificatif de l'acheteur.
    assert "Karim" not in client.get(f"/runner/listings/{transfer.listing_id}").text


def test_failed_analysis_shows_the_error_and_saves_nothing(client, session, transfer, reader):
    set_state(session, transfer, TransferState.AUTORISE)
    reader["error"] = AIError("Le service d'analyse est injoignable : vérifie la connexion et réessaie.")

    response = client.post(f"/runner/transfers/{transfer.id}/proof", files=PROOF)

    assert response.status_code == 422
    assert "injoignable" in response.text
    assert session.exec(select(Verification)).first() is None
