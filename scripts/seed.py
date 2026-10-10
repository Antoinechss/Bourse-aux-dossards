"""Remplit la base avec les courses de démo décrites dans seed/demo.json.

À lancer depuis la racine du projet : uv run python -m scripts.seed
"""

import json
from pathlib import Path

from sqlmodel import Session, select

from app.config import settings
from app.db import create_db_and_tables, engine
from app.models import Race, RaceRulesRow, Registration

DATA_FILE = Path(__file__).parent.parent / "seed" / "demo.json"


def seed(session: Session, organizer_email: str) -> int:
    """Insère les courses de démo et renvoie leur nombre. Ne fait rien si la base contient déjà une course."""
    if session.exec(select(Race)).first() is not None:
        return 0
    races = json.loads(DATA_FILE.read_text(encoding="utf-8"))["races"]
    for item in races:
        # model_validate convertit les dates, écrites en texte dans le JSON.
        race = Race.model_validate(
            {"name": item["name"], "event_date": item["event_date"], "organizer_email": organizer_email}
        )
        session.add(race)
        session.flush()  # attribue race.id sans encore valider la transaction
        session.add(RaceRulesRow.model_validate({**item["rules"], "race_id": race.id}))
        session.add_all(Registration.model_validate({**r, "race_id": race.id}) for r in item["registrations"])
    session.commit()
    return len(races)


def main() -> None:
    if not settings.demo_organizer_email:
        raise SystemExit("DEMO_ORGANIZER_EMAIL est vide : renseigne-le dans .env")
    create_db_and_tables()
    with Session(engine) as session:
        count = seed(session, settings.demo_organizer_email)
    if count:
        print(f"{count} courses de démo ajoutées.")
    else:
        print("La base contient déjà des courses : rien à faire.")


if __name__ == "__main__":
    main()
