"""Registre simulé de l'organisateur : lister les inscrits d'une course, changer le nom d'un dossard."""

from sqlmodel import Session, select

from app.models import Registration


def list_registrations(session: Session, race_id: int) -> list[Registration]:
    statement = select(Registration).where(Registration.race_id == race_id).order_by(Registration.bib_number)
    return list(session.exec(statement))


def get_registration(session: Session, race_id: int, bib_number: int) -> Registration | None:
    statement = select(Registration).where(
        Registration.race_id == race_id, Registration.bib_number == bib_number
    )
    return session.exec(statement).first()


def change_name(
    session: Session, race_id: int, bib_number: int, first_name: str, last_name: str
) -> Registration | None:
    """Met le dossard au nouveau nom. Renvoie None si ce dossard n'existe pas dans la course."""
    registration = get_registration(session, race_id, bib_number)
    if registration is None:
        return None
    registration.first_name = first_name
    registration.last_name = last_name
    session.add(registration)
    session.commit()
    session.refresh(registration)
    return registration
