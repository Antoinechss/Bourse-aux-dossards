"""Boîte mail de démo : affiche les notifications au lieu d'envoyer de vrais emails."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlmodel import select

from app.db import SessionDep
from app.models import Notification
from app.templating import templates

router = APIRouter(tags=["inbox"])


@router.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request, session: SessionDep, email: str = ""):
    """Tous les messages, du plus récent au plus ancien ; `email` ne garde que ceux d'un destinataire."""
    email = email.strip().lower()
    statement = select(Notification).order_by(Notification.id.desc()).limit(200)
    if email:
        statement = statement.where(Notification.recipient_email == email)
    context = {
        "notifications": session.exec(statement).all(),
        "recipients": sorted(set(session.exec(select(Notification.recipient_email)))),
        "email": email,
    }
    return templates.TemplateResponse(request, "inbox.html", context)
