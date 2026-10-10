"""Le parcours complet d'un dossard, par les pages : vendre, attendre, acheter, suivre.

PayPal et l'IA sont remplacés par des faux ; tout le reste est le vrai code.
"""

from datetime import date

from paypal_fakes import FakePayPal
from sqlmodel import select

from app.ai.verifier import DocumentType, ProofReading
from app.models import Notification, RaceRulesRow, Registration, Transfer
from app.registry_mock.registry import get_registration
from app.transfers import flow, verification
from app.transfers.state import TransferState


def test_a_bib_goes_from_its_seller_to_a_verified_buyer(client, session, race, monkeypatch):
    FakePayPal().install(monkeypatch)
    licence = ProofReading(
        document_type=DocumentType.LICENCE_FFA, holder_name="MARTIN Marie", valid_until=date(2027, 8, 31), concern=None
    )
    monkeypatch.setattr(verification, "read_proof", lambda data, client=None: licence)
    session.add_all(
        [
            RaceRulesRow(race_id=race.id, transfer_allowed=True, max_resale_price_cents=4500),
            Registration(race_id=race.id, bib_number=42, first_name="Jean", last_name="Dupont"),
        ]
    )
    session.commit()

    # Marie attend un dossard ; Jean met le sien en vente : il lui est proposé.
    client.post(
        f"/runner/races/{race.id}/waitlist",
        data={"first_name": "Marie", "last_name": "Martin", "email": "marie@example.com"},
    )
    client.post(
        f"/runner/races/{race.id}/listings",
        data={"bib_number": "42", "first_name": "Jean", "last_name": "Dupont", "email": "jean@example.com", "price": "45"},
    )
    transfer = session.exec(select(Transfer)).one()
    assert transfer.state is TransferState.RESERVE

    # Marie paie (autorisation), puis dépose sa licence : l'IA est au vert.
    assert client.post(f"/runner/transfers/{transfer.id}/order").status_code == 200
    assert client.post(f"/runner/transfers/{transfer.id}/authorize").json() == {"state": "AUTORISE"}
    proof = {"justificatif": ("licence.png", b"\x89PNG\r\n\x1a\n une image", "image/png")}
    assert client.post(f"/runner/transfers/{transfer.id}/proof", files=proof).status_code == 204
    assert session.get(Transfer, transfer.id).state is TransferState.VERIFIE

    # Le ménage périodique fait le reste : nom, encaissement, versement.
    flow.housekeeping(session)

    assert session.get(Transfer, transfer.id).state is TransferState.VERSE
    holder = get_registration(session, race.id, 42)
    assert (holder.first_name, holder.last_name) == ("Marie", "Martin")
    assert "Paiement encaissé : le dossard est à votre nom." in client.get(f"/runner/transfers/{transfer.id}").text
    assert "Vous avez été payé : 45,00 €" in client.get(f"/runner/listings/{transfer.listing_id}").text

    # Chacun a reçu ses messages, visibles dans la boîte mail de démo.
    def subjects(email):
        statement = select(Notification.subject).where(Notification.recipient_email == email).order_by(Notification.id)
        return [subject.split(" — ")[0] for subject in session.exec(statement)]

    assert subjects("marie@example.com") == [
        "Vous êtes sur la liste d'attente",
        "Un dossard est disponible pour vous",
        "Paiement autorisé : déposez votre justificatif",
        "Justificatif validé",
        "Le dossard n°42 est à votre nom",
    ]
    assert subjects("jean@example.com") == [
        "Votre dossard n°42 est en vente",
        "Un acheteur a payé votre dossard n°42",
        "Vous avez été payé",
    ]
    assert "Vous avez été payé" in client.get("/inbox", params={"email": "jean@example.com"}).text
