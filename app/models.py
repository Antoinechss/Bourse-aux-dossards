"""Tables de la base (SQLModel). Montants en centimes entiers, dates en UTC."""

from datetime import date, datetime, timezone
from enum import StrEnum

from sqlalchemy import JSON, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.transfers.state import TransferState


def utcnow() -> datetime:
    # Toujours avec fuseau : SQLModel refuse d'enregistrer une date qui n'en a pas.
    return datetime.now(timezone.utc)


class Verdict(StrEnum):
    VERT = "VERT"
    ORANGE = "ORANGE"
    ROUGE = "ROUGE"


class ProofType(StrEnum):
    PPS = "PPS"
    LICENCE_FFA = "LICENCE_FFA"


class LedgerKind(StrEnum):
    AUTORISATION = "AUTORISATION"
    ANNULATION = "ANNULATION"
    CAPTURE = "CAPTURE"
    VERSEMENT_VENDEUR = "VERSEMENT_VENDEUR"
    VERSEMENT_ORGANISATEUR = "VERSEMENT_ORGANISATEUR"


class Race(SQLModel, table=True):
    """Une course ouverte à la revente de dossards."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    event_date: date
    distance: str = ""  # telle qu'on l'imprime sur le dossard : "10 km", "21,1 km"
    minimum_age: int = 0  # âge requis le jour de la course ; 0 s'il n'y en a pas
    organizer_email: str  # destinataire du Payout de la part organisateur
    # Commission en points de base (500 = 5 %), payée par l'acheteur en plus du prix.
    platform_fee_bps: int = 500
    organizer_fee_bps: int = 300


QUOTE = (
    "Phrase exacte du règlement qui justifie la valeur précédente, précédée du numéro de son article "
    "(exemple : « Article 9 — Aucun transfert n'est accepté après le 28 mars 2027. »). "
    "null si la valeur précédente est null."
)


class RaceRules(SQLModel):
    """Ce que le règlement d'une course dit de la revente des dossards.

    C'est aussi le format de réponse demandé à l'IA (app/ai/rules_reader.py) : les descriptions
    ci-dessous lui sont envoyées. Chaque règle va avec la citation qui la justifie (_quote) ;
    les deux valent None si le règlement n'en parle pas.
    """

    transfer_allowed: bool | None = Field(
        description="true si le règlement autorise le transfert, la cession ou la revente d'un dossard à un autre "
        "coureur ; false s'il l'interdit ; null s'il n'en parle pas."
    )
    transfer_allowed_quote: str | None = Field(description=QUOTE)
    transfer_deadline: date | None = Field(
        description="Dernier jour où un transfert de dossard est accepté. null si le règlement ne fixe pas de date "
        "limite pour les transferts : une date limite d'inscription ou de remboursement n'en est pas une."
    )
    transfer_deadline_quote: str | None = Field(description=QUOTE)
    max_resale_price_cents: int | None = Field(
        description="Prix maximum auquel un dossard peut être revendu, en centimes d'euro (45 € = 4500). null si le "
        "règlement ne chiffre pas de plafond : les tarifs d'inscription ne sont pas un plafond de revente."
    )
    max_resale_price_quote: str | None = Field(description=QUOTE)
    transfer_fee_cents: int | None = Field(
        description="Frais facturés par l'organisateur pour un transfert, en centimes d'euro (5 € = 500 ; 0 si le "
        "règlement dit que le transfert est gratuit). null si le règlement ne parle pas de frais de transfert."
    )
    transfer_fee_quote: str | None = Field(description=QUOTE)
    required_proofs: list[ProofType] | None = Field(
        sa_type=JSON,
        description="Justificatifs acceptés pour prendre le départ, parmi PPS (Parcours de Prévention Santé) et "
        "LICENCE_FFA (licence de la Fédération Française d'Athlétisme). null si le règlement n'exige ni l'un ni l'autre.",
    )
    required_proofs_quote: str | None = Field(description=QUOTE)


class RaceRulesRow(RaceRules, table=True):
    """Les règles d'une course, une fois confirmées par l'organisateur."""

    __tablename__ = "racerules"

    id: int | None = Field(default=None, primary_key=True)
    race_id: int = Field(foreign_key="race.id", unique=True)


class Registration(SQLModel, table=True):
    """Un inscrit dans le registre (simulé) de l'organisateur."""

    __table_args__ = (UniqueConstraint("race_id", "bib_number"),)

    id: int | None = Field(default=None, primary_key=True)
    race_id: int = Field(foreign_key="race.id", index=True)
    bib_number: int
    first_name: str
    last_name: str


class Listing(SQLModel, table=True):
    """Une annonce : un coureur met son dossard en vente."""

    id: int | None = Field(default=None, primary_key=True)
    race_id: int = Field(foreign_key="race.id", index=True)
    # Le dossard est désigné par (course, numéro), comme dans le registre de l'organisateur.
    bib_number: int
    seller_email: str  # sert aussi de destinataire du Payout
    price_cents: int
    created_at: datetime = Field(default_factory=utcnow)


class WaitlistEntry(SQLModel, table=True):
    """Un coureur qui veut être prévenu quand un dossard est mis en vente pour une course."""

    id: int | None = Field(default=None, primary_key=True)
    race_id: int = Field(foreign_key="race.id", index=True)
    first_name: str
    last_name: str
    email: str
    created_at: datetime = Field(default_factory=utcnow)


class Buyer(SQLModel, table=True):
    """La personne qui achète un dossard. Son identité sert aux contrôles faits avant tout paiement."""

    id: int | None = Field(default=None, primary_key=True)
    race_id: int = Field(foreign_key="race.id", index=True)
    first_name: str
    last_name: str
    email: str
    birth_date: date | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Transfer(SQLModel, table=True):
    """Une tentative de vente d'une annonce à un acheteur.

    Si elle s'arrête sans vente (réservation expirée, acheteur inéligible), le dossard repart en vente
    dans un nouveau Transfer : les identifiants PayPal d'un acheteur ne servent ainsi jamais à un autre.
    """

    id: int | None = Field(default=None, primary_key=True)
    listing_id: int = Field(foreign_key="listing.id", index=True)
    state: TransferState = TransferState.DOSSARD_VERIFIE
    # L'acheteur qui a réservé le dossard ; None tant qu'il n'a pas donné son identité.
    buyer_id: int | None = Field(default=None, foreign_key="buyer.id")
    offer_expires_at: datetime | None = None  # fin de la réservation : passé ce moment, le dossard repart en vente
    paypal_order_id: str | None = None
    paypal_authorization_id: str | None = None
    authorized_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Verification(SQLModel, table=True):
    """Le résultat de l'analyse d'un justificatif. Le fichier, lui, n'est jamais conservé."""

    id: int | None = Field(default=None, primary_key=True)
    transfer_id: int = Field(foreign_key="transfer.id", index=True)
    proof_type: ProofType | None = None  # None si le document n'est ni un PPS ni une licence
    verdict: Verdict
    explanation: str
    # Décision de l'organisateur quand l'IA n'est pas au vert ; None tant qu'il n'a pas tranché.
    organizer_approved: bool | None = None
    created_at: datetime = Field(default_factory=utcnow)


class LedgerEntry(SQLModel, table=True):
    """Une ligne du journal comptable : un mouvement d'argent sur un transfert."""

    id: int | None = Field(default=None, primary_key=True)
    transfer_id: int = Field(foreign_key="transfer.id", index=True)
    kind: LedgerKind
    amount_cents: int
    currency: str = "EUR"
    paypal_id: str | None = None  # identifiant PayPal du mouvement
    created_at: datetime = Field(default_factory=utcnow)


class Notification(SQLModel, table=True):
    """Un message envoyé à un coureur. En démo, il s'affiche sur /inbox au lieu de partir par email."""

    id: int | None = Field(default=None, primary_key=True)
    recipient_email: str = Field(index=True)
    subject: str
    body: str
    link: str | None = None  # page de Relais à ouvrir depuis le message
    created_at: datetime = Field(default_factory=utcnow)


class WebhookEvent(SQLModel, table=True):
    """Un événement PayPal déjà traité : PayPal peut envoyer plusieurs fois le même."""

    id: int | None = Field(default=None, primary_key=True)
    paypal_event_id: str = Field(unique=True)
    event_type: str
    received_at: datetime = Field(default_factory=utcnow)
