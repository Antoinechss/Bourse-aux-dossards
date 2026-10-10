"""Journal comptable : chaque mouvement d'argent sur un transfert y laisse une ligne."""

from sqlmodel import Session, select

from app.models import LedgerEntry, LedgerKind


def record(
    session: Session, transfer_id: int, kind: LedgerKind, amount_cents: int, paypal_id: str
) -> LedgerEntry:
    """Ajoute une ligne à la session. C'est l'appelant qui enregistre (commit)."""
    entry = LedgerEntry(transfer_id=transfer_id, kind=kind, amount_cents=amount_cents, paypal_id=paypal_id)
    session.add(entry)
    return entry


def find(session: Session, transfer_id: int, kind: LedgerKind) -> LedgerEntry | None:
    """La ligne de ce type pour ce transfert, ou None si ce mouvement n'a pas eu lieu."""
    statement = select(LedgerEntry).where(LedgerEntry.transfer_id == transfer_id, LedgerEntry.kind == kind)
    return session.exec(statement).first()


def entries(session: Session, transfer_id: int) -> list[LedgerEntry]:
    """Toutes les lignes d'un transfert, dans l'ordre où elles ont été écrites."""
    statement = select(LedgerEntry).where(LedgerEntry.transfer_id == transfer_id).order_by(LedgerEntry.id)
    return list(session.exec(statement))
