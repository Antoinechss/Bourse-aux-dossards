"""Webhooks PayPal : PayPal nous prévient quand un encaissement ou un versement a abouti."""

import json
import logging
import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from app.db import SessionDep
from app.models import Transfer, WebhookEvent
from app.payments import service
from app.payments.client import PayPalError
from app.payments.webhooks import verify_signature
from app.transfers import flow

router = APIRouter(tags=["webhooks"])
logger = logging.getLogger(__name__)

# La référence que nous donnons à PayPal pour chaque transfert : relais-<numéro>-<date de création>.
REFERENCE = re.compile(r"relais-(\d+)-\d{14}")


async def raw_body(request: Request) -> bytes:
    """Le corps exact de la requête : c'est lui que PayPal a signé."""
    return await request.body()


def find_transfer(session: Session, text: str | None) -> Transfer | None:
    """Retrouve le transfert dont la référence figure dans ce texte venu de PayPal."""
    match = REFERENCE.search(text or "")
    if match is None:
        return None
    transfer = session.get(Transfer, int(match.group(1)))
    # La date de création écarte un transfert de même numéro venu d'une autre base.
    if transfer is None or service.transfer_reference(transfer) != match.group(0):
        return None
    return transfer


def handle(session: Session, event: dict) -> str:
    """Traite un événement dont la signature est vérifiée. Renvoie "processed" ou "ignored"."""
    event_type = event.get("event_type")
    resource = event.get("resource") or {}

    if event_type == "PAYMENT.CAPTURE.COMPLETED":
        transfer = find_transfer(session, resource.get("custom_id"))
        if transfer is None:
            return "ignored"
        flow.advance(session, transfer)
        return "processed"

    if event_type == "PAYMENT.PAYOUTSBATCH.SUCCESS":
        sender = (resource.get("batch_header") or {}).get("sender_batch_header") or {}
        transfer = find_transfer(session, sender.get("sender_batch_id"))
        if transfer is None:
            return "ignored"
        # Le webhook sert de signal : l'état de chaque versement est relu chez PayPal avant de confirmer.
        return "processed" if flow.check_payout(session, transfer) else "ignored"

    return "ignored"


@router.post("/webhooks/paypal")
def paypal_webhook(request: Request, session: SessionDep, body: Annotated[bytes, Depends(raw_body)]):
    try:
        event = json.loads(body)
    except ValueError:
        event = None
    if not isinstance(event, dict) or not event.get("id"):
        raise HTTPException(status_code=400, detail="Événement illisible")

    # Règle du projet : la signature est vérifiée avant de traiter quoi que ce soit.
    try:
        genuine = verify_signature(request.headers, body)
    except (RuntimeError, PayPalError) as error:
        # Mal configuré ou PayPal injoignable : on ne sait pas. 503 demande à PayPal de renvoyer plus tard.
        logger.error("Webhook %s : signature invérifiable (%s)", event["id"], error)
        raise HTTPException(status_code=503, detail="Signature invérifiable pour l'instant") from error
    if not genuine:
        logger.warning("Webhook %s refusé : signature invalide", event["id"])
        raise HTTPException(status_code=400, detail="Signature invalide")

    with flow.LOCK:
        already = session.exec(select(WebhookEvent).where(WebhookEvent.paypal_event_id == event["id"])).first()
        if already:
            return {"status": "duplicate"}
        try:
            status = handle(session, event)
        except Exception as error:
            # L'événement n'est pas noté comme traité : PayPal le renverra, et le traitement sera retenté.
            session.rollback()
            logger.exception("Webhook %s : traitement en échec", event["id"])
            raise HTTPException(status_code=500, detail="Traitement en échec") from error
        session.add(WebhookEvent(paypal_event_id=event["id"], event_type=event.get("event_type") or ""))
        session.commit()
    return {"status": status}
