"""Remplit la base avec les courses de démo décrites dans seed/demo.json.

À lancer depuis la racine du projet : uv run python -m scripts.seed
"""

import json
import os
from pathlib import Path

from sqlmodel import Session, select

from app.config import settings
from app.db import create_db_and_tables, engine
from app.marketplace import service as market
from app.models import Race, RaceRulesRow, Registration
from app.registry_mock.registry import get_registration

DATA_FILE = Path(__file__).parent.parent / "seed" / "demo.json"


def seed(session: Session, organizer_email: str, seller_emails: list[str] | None = None) -> int:
    """Insère les courses de démo et renvoie leur nombre. Ne fait rien si la base contient déjà une course.

    Chaque course arrive avec ses règles, ses inscrits et quelques dossards déjà en vente. Ceux-ci passent
    par la vraie mise en vente : ils sont donc vérifiés auprès du registre comme n'importe quel dossard.
    `seller_emails` sont les comptes qui recevront l'argent de ces ventes, à tour de rôle.
    """
    if session.exec(select(Race)).first() is not None:
        return 0
    sellers = seller_emails or ["vendeur.demo@example.com"]
    races = json.loads(DATA_FILE.read_text(encoding="utf-8"))["races"]
    on_sale = 0
    for item in races:
        # model_validate convertit les dates, écrites en texte dans le JSON.
        race = Race.model_validate(
            {
                "name": item["name"],
                "event_date": item["event_date"],
                "distance": item["distance"],
                "minimum_age": item["minimum_age"],
                "organizer_email": organizer_email,
            }
        )
        session.add(race)
        session.flush()  # attribue race.id sans encore valider la transaction
        session.add(RaceRulesRow.model_validate({**item["rules"], "race_id": race.id}))
        session.add_all(Registration.model_validate({**r, "race_id": race.id}) for r in item["registrations"])
        session.commit()
        for listing in item["listings"]:
            holder = get_registration(session, race.id, listing["bib_number"])
            market.create_listing(
                session,
                race,
                bib_number=listing["bib_number"],
                first_name=holder.first_name,
                last_name=holder.last_name,
                seller_email=sellers[on_sale % len(sellers)],
                price_cents=listing["price_cents"],
            )
            on_sale += 1
    return len(races)


def main() -> None:
    if not settings.demo_organizer_email:
        raise SystemExit("DEMO_ORGANIZER_EMAIL est vide : renseigne-le dans .env")
    sellers = [email.strip().lower() for email in os.getenv("DEMO_RUNNER_EMAILS", "").split(",") if email.strip()]
    create_db_and_tables()
    with Session(engine) as session:
        count = seed(session, settings.demo_organizer_email, sellers)
    if count:
        print(f"{count} courses de démo ajoutées, avec quelques dossards en vente.")
        if not sellers:
            print("DEMO_RUNNER_EMAILS est vide : les vendeurs de démo n'ont pas de compte PayPal où recevoir leur vente.")
    else:
        print("La base contient déjà des courses : rien à faire.")


if __name__ == "__main__":
    main()
