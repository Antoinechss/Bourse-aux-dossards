"""Notifications aux coureurs.

En démo, elles sont rangées en base et affichées sur /inbox : aucun email ne part.
"""

from sqlmodel import Session

from app.models import Notification


def notify(session: Session, to: str, subject: str, body: str, link: str | None = None) -> Notification:
    """Ajoute le message à la session. C'est l'appelant qui enregistre (commit)."""
    notification = Notification(recipient_email=to.lower(), subject=subject, body=body, link=link)
    session.add(notification)
    return notification
