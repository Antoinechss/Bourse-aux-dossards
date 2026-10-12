"""Faux PayPal pour les tests : remplace les appels de app/payments/orders.py et payouts.py."""

from sqlmodel import Session

from app.models import Listing, Race, Transfer, Buyer
from app.payments import orders, payouts
from app.transfers.state import TransferState


class FakePayPal:
    """Répond comme PayPal quand tout va bien, et retient ce qui lui a été demandé."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []  # (opération, clé d'idempotence)
        self.order_request: dict = {}
        self.payout_items: list[payouts.PayoutItem] = []
        self.authorization_status = "CREATED"
        self.capture_status = "COMPLETED"
        self.batch_status = "PENDING"
        self.error: Exception | None = None  # si renseignée, chaque appel échoue avec cette erreur

    def install(self, monkeypatch) -> None:
        """Branche le faux à la place des vrais appels, le temps d'un test."""
        monkeypatch.setattr(orders, "create_order", self.create_order)
        monkeypatch.setattr(orders, "get_order", self.get_order)
        monkeypatch.setattr(orders, "authorize_order", self.authorize_order)
        monkeypatch.setattr(orders, "capture_authorization", self.capture_authorization)
        monkeypatch.setattr(orders, "void_authorization", self.void_authorization)
        monkeypatch.setattr(payouts, "send_payouts", self.send_payouts)
        monkeypatch.setattr(payouts, "get_payout_batch", self.get_payout_batch)

    def _record(self, operation: str, request_id: str) -> None:
        self.calls.append((operation, request_id))
        if self.error is not None:
            raise self.error

    def create_order(self, *, request_id, **kwargs):
        self._record("create_order", request_id)
        self.order_request = kwargs
        return orders.Order(id="ORDER-1", status="PAYER_ACTION_REQUIRED", approve_url="https://paypal.test/ok")

    def get_order(self, order_id):
        # L'acheteur a approuvé : c'est ce que le script attend en relisant la commande.
        return orders.Order(id=order_id, status="APPROVED", approve_url=None)

    def authorize_order(self, order_id, *, request_id):
        self._record("authorize_order", request_id)
        return orders.Authorization(id="AUTH-1", status=self.authorization_status, amount_cents=4860)

    def capture_authorization(self, authorization_id, *, request_id):
        self._record("capture_authorization", request_id)
        return orders.Capture(id="CAPT-1", status=self.capture_status, amount_cents=4860)

    def void_authorization(self, authorization_id, *, request_id):
        self._record("void_authorization", request_id)

    def send_payouts(self, items, *, request_id):
        self._record("send_payouts", request_id)
        self.payout_items = items
        return payouts.PayoutBatch(id="BATCH-1", status=self.batch_status)

    def get_payout_batch(self, batch_id):
        # PayPal a fini de traiter le lot.
        statuses = {item.item_id: "SUCCESS" for item in self.payout_items}
        return payouts.PayoutBatch(id=batch_id, status="SUCCESS", item_statuses=statuses)

    def operations(self) -> list[str]:
        return [operation for operation, _ in self.calls]


def reserved_transfer(session: Session, race: Race) -> Transfer:
    """Un dossard à 45 € proposé à une acheteuse : le transfert attend son paiement."""
    listing = Listing(race_id=race.id, bib_number=42, seller_email="vendeur@example.com", price_cents=4500)
    buyer = Buyer(race_id=race.id, first_name="Marie", last_name="Martin", email="marie@example.com")
    session.add_all([listing, buyer])
    session.commit()
    transfer = Transfer(listing_id=listing.id, buyer_id=buyer.id, state=TransferState.ELIGIBLE)
    session.add(transfer)
    session.commit()
    return transfer
