"""Déroule un cycle de paiement complet dans la sandbox PayPal, sans interface.

À lancer depuis la racine du projet :

    uv run python -m scripts.sandbox_cycle             # autorisé, encaissé, puis versé
    uv run python -m scripts.sandbox_cycle --refuser   # autorisé puis annulé : acheteur jamais débité

Le script passe par app/payments/service.py, comme le fera l'application, avec une base
temporaire en mémoire : relais.db n'est pas touchée. Une seule étape est manuelle : approuver
le paiement dans un navigateur avec un compte sandbox « Personal », qui joue l'acheteur.
"""

import argparse
import os
import sys
import time
from datetime import date

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.config import settings
from app.models import Listing, Race, Transfer, Buyer
from app.payments import ledger, orders, payouts, service
from app.payments.client import PayPalError, PayPalUnreachable
from app.payments.money import format_euros
from app.transfers.state import TransferEvent, TransferState, next_state

# Après l'approbation, PayPal renvoie le navigateur vers cette adresse. Personne ne l'écoute :
# le script apprend l'approbation en relisant la commande.
RETURN_URL = "https://example.com/relais/paiement-approuve"
CANCEL_URL = "https://example.com/relais/paiement-abandonne"


def demo_transfer(session: Session, seller_email: str, organizer_email: str) -> Transfer:
    """Un dossard à 45 € réservé par une acheteuse dont les contrôles sont passés (état ELIGIBLE)."""
    race = Race(name="Trail des Crêtes de Valombre", event_date=date(2027, 4, 11), organizer_email=organizer_email)
    session.add(race)
    session.commit()
    listing = Listing(race_id=race.id, bib_number=104, seller_email=seller_email, price_cents=4500)
    buyer = Buyer(race_id=race.id, first_name="Marie", last_name="Martin", email="marie@example.com")
    session.add_all([listing, buyer])
    session.commit()
    transfer = Transfer(listing_id=listing.id, buyer_id=buyer.id, state=TransferState.ELIGIBLE)
    session.add(transfer)
    session.commit()
    return transfer


def advance(session: Session, transfer: Transfer, event: TransferEvent) -> None:
    """Fait avancer la machine à états, comme le fera app/transfers/."""
    transfer.state = next_state(transfer.state, event)
    session.add(transfer)
    session.commit()
    print(f"   état du transfert : {transfer.state}")


def retry(step, *args, attempts: int = 3, **kwargs):
    """Rejoue une étape si PayPal ne répond pas : sans danger, sa clé d'idempotence ne change pas."""
    for attempt in range(1, attempts + 1):
        try:
            return step(*args, **kwargs)
        except PayPalUnreachable:
            if attempt == attempts:
                raise
            print("   PayPal n'a pas répondu, nouvel essai...")
            time.sleep(2)


def wait_for_approval(order_id: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if orders.get_order(order_id).status == "APPROVED":
                return
        except PayPalUnreachable:
            pass  # coupure réseau passagère : on relira la commande au tour suivant
        time.sleep(3)
    raise SystemExit(f"Paiement non approuvé au bout de {timeout_seconds} s : rien n'a été bloqué ni débité.")


def wait_for_payouts(batch_id: str, timeout_seconds: int = 120) -> payouts.PayoutBatch | None:
    """Relit le lot jusqu'à ce que PayPal l'ait traité. Renvoie son dernier état connu."""
    deadline = time.monotonic() + timeout_seconds
    batch = None
    while time.monotonic() < deadline:
        try:
            batch = payouts.get_payout_batch(batch_id)
            if batch.status in ("SUCCESS", "DENIED", "CANCELED"):
                break
        except PayPalUnreachable:
            pass
        time.sleep(4)
    return batch


def print_ledger(session: Session, transfer: Transfer) -> None:
    print("\nJournal comptable")
    for entry in ledger.entries(session, transfer.id):
        print(f"   {entry.kind:<24}{format_euros(entry.amount_cents):>10}   {entry.paypal_id}")


def run(refuse: bool, approval_timeout: int) -> None:
    runner_emails = [email.strip() for email in os.getenv("DEMO_RUNNER_EMAILS", "").split(",") if email.strip()]
    if not settings.demo_organizer_email or not runner_emails:
        raise SystemExit("Renseigne DEMO_ORGANIZER_EMAIL et DEMO_RUNNER_EMAILS dans .env (comptes sandbox).")
    seller_email = runner_emails[0]

    # Base SQLite en mémoire, jetée à la fin du script.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        transfer = demo_transfer(session, seller_email, settings.demo_organizer_email)
        amounts = service.amounts_for(session, transfer)
        print("Dossard n°104 — Trail des Crêtes de Valombre")
        print(f"   prix du dossard    {format_euros(amounts.price_cents):>10}   -> vendeur ({seller_email})")
        print(f"   frais de service   {format_euros(amounts.service_fee_cents):>10}   dont")
        print(f"      organisateur    {format_euros(amounts.organizer_fee_cents):>10}   ({settings.demo_organizer_email})")
        print(f"      plateforme      {format_euros(amounts.platform_fee_cents):>10}")
        print(f"   total acheteur     {format_euros(amounts.total_cents):>10}")
        print(f"   état du transfert : {transfer.state}")

        print("\n1. Création de la commande PayPal (intent AUTHORIZE)")
        order = retry(service.create_order, session, transfer, return_url=RETURN_URL, cancel_url=CANCEL_URL)
        print(f"   commande {order.id}, statut {order.status}")

        print("\n2. Approbation par l'acheteur")
        suggestion = f" (par exemple {runner_emails[1]})" if len(runner_emails) > 1 else ""
        print(f"   Ouvre ce lien et connecte-toi avec un compte sandbox « Personal » autre que le vendeur{suggestion} :")
        print(f"\n   {order.approve_url}\n")
        print("   Après « Payer », le navigateur arrive sur example.com : c'est normal, reviens ici.")
        print(f"   En attente de l'approbation ({approval_timeout} s maximum)...")
        wait_for_approval(order.id, approval_timeout)
        print("   commande approuvée")

        print("\n3. Autorisation : les fonds sont bloqués, l'acheteur n'est pas débité")
        authorized = retry(service.authorize, session, transfer)
        print(f"   autorisation {authorized.paypal_id} de {format_euros(authorized.amount_cents)}")
        advance(session, transfer, TransferEvent.AUTORISER)

        if refuse:
            print("\n4. Justificatif refusé : annulation de l'autorisation (void)")
            retry(service.void, session, transfer)
            print("   fonds libérés, l'acheteur n'a jamais été débité")
            advance(session, transfer, TransferEvent.REFUSER)
            print_ledger(session, transfer)
            return

        print("\n4. Transfert confirmé par l'organisateur : le dossard passe au nom de l'acheteur (simulé ici)")
        advance(session, transfer, TransferEvent.CHANGER_NOM)

        print("\n5. Capture : l'acheteur est débité, la plateforme encaisse")
        captured = retry(service.capture, session, transfer)
        print(f"   capture {captured.paypal_id} de {format_euros(captured.amount_cents)}")
        advance(session, transfer, TransferEvent.ENCAISSER)

        print("\n6. Payouts : versement au vendeur et à l'organisateur")
        paid = retry(service.pay_out, session, transfer)
        print(f"   lot {paid[0].paypal_id} envoyé, attente du traitement par PayPal...")
        batch = wait_for_payouts(paid[0].paypal_id)
        for item_id, status in (batch.item_statuses if batch else {}).items():
            print(f"   {item_id} : {status}")
        if batch is None or batch.status != "SUCCESS":
            status = batch.status if batch else "inconnu"
            raise SystemExit(f"Lot {paid[0].paypal_id} en statut {status} : à vérifier dans le tableau de bord PayPal.")
        advance(session, transfer, TransferEvent.VERSER)

        print_ledger(session, transfer)
        print(f"\nReste à la plateforme : {format_euros(amounts.platform_fee_cents)} (avant les frais PayPal)")


def main() -> None:
    # Affiche chaque ligne tout de suite, même quand la sortie est redirigée vers un fichier.
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description="Cycle de paiement complet dans la sandbox PayPal.")
    parser.add_argument("--refuser", action="store_true", help="annule l'autorisation au lieu de capturer")
    parser.add_argument("--attente", type=int, default=600, help="secondes laissées pour approuver (défaut : 600)")
    args = parser.parse_args()
    try:
        run(refuse=args.refuser, approval_timeout=args.attente)
    except (PayPalError, service.PaymentError) as error:
        # « from None » : le message suffit, inutile d'afficher toute la pile d'appels.
        raise SystemExit(f"\nÉchec : {error}") from None


if __name__ == "__main__":
    main()
