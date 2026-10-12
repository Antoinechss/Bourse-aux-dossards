"""Crée une offre de démo dans la base locale et affiche l'adresse de sa page d'achat.

À lancer depuis la racine du projet, après scripts.seed :

    uv run python -m scripts.demo_offer

En attendant le marketplace (annonces, liste d'attente, offres), c'est le moyen d'essayer la
page d'achat : un dossard de la première course est mis en vente, puis proposé à une acheteuse.
"""

import os
from datetime import timedelta

from sqlmodel import Session, select

from app.db import create_db_and_tables, engine
from app.models import Listing, Race, RaceRulesRow, Registration, Transfer, Buyer, utcnow
from app.transfers.state import TransferState

PRICE_CENTS = 4500


def create_offer(session: Session, seller_email: str) -> Transfer:
    """Met en vente le premier dossard encore libre de la première course, et le propose à une acheteuse."""
    race = session.exec(select(Race).order_by(Race.id)).first()
    if race is None:
        raise SystemExit("Aucune course en base : lance d'abord  uv run python -m scripts.seed")
    listed_bibs = set(session.exec(select(Listing.bib_number).where(Listing.race_id == race.id)).all())
    registrations = session.exec(
        select(Registration).where(Registration.race_id == race.id).order_by(Registration.bib_number)
    ).all()
    registration = next((r for r in registrations if r.bib_number not in listed_bibs), None)
    if registration is None:
        raise SystemExit(f"Tous les dossards de « {race.name} » sont déjà en vente.")

    # Le prix de revente respecte le plafond du règlement, s'il y en a un.
    rules = session.exec(select(RaceRulesRow).where(RaceRulesRow.race_id == race.id)).first()
    cap_cents = rules.max_resale_price_cents if rules else None
    listing = Listing(
        race_id=race.id,
        bib_number=registration.bib_number,
        seller_email=seller_email,
        price_cents=min(PRICE_CENTS, cap_cents) if cap_cents else PRICE_CENTS,
    )
    buyer = Buyer(race_id=race.id, first_name="Marie", last_name="Martin", email="marie@example.com")
    session.add_all([listing, buyer])
    session.commit()
    transfer = Transfer(
        listing_id=listing.id,
        buyer_id=buyer.id,
        state=TransferState.ELIGIBLE,  # réservé, contrôles passés : il ne reste qu'à payer
        offer_expires_at=utcnow() + timedelta(hours=48),
    )
    session.add(transfer)
    session.commit()
    return transfer


def main() -> None:
    runner_emails = [email.strip() for email in os.getenv("DEMO_RUNNER_EMAILS", "").split(",") if email.strip()]
    if not runner_emails:
        raise SystemExit("DEMO_RUNNER_EMAILS est vide : renseigne-le dans .env (le premier coureur sera le vendeur)")
    create_db_and_tables()
    with Session(engine) as session:
        transfer = create_offer(session, runner_emails[0])
        print("Offre créée. Lance le serveur (uv run fastapi dev app/main.py) puis ouvre :")
        print(f"   http://127.0.0.1:8000/runner/transfers/{transfer.id}/checkout")


if __name__ == "__main__":
    main()
