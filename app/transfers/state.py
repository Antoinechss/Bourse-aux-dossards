"""Machine à états d'un transfert : fonction pure, sans base de données ni PayPal.

L'ordre garantit deux choses : un dossard n'est en vente qu'une fois vérifié auprès de l'organisateur,
et l'acheteur n'est contrôlé puis débité que dans cet ordre, jamais l'inverse.
"""

from enum import StrEnum


class TransferState(StrEnum):
    DOSSARD_VERIFIE = "DOSSARD_VERIFIE"  # l'organisateur confirme que le dossard est bien celui du vendeur
    EN_VENTE = "EN_VENTE"
    RESERVE = "RESERVE"  # un acheteur a cliqué « Acheter ce dossard » : il a quelques minutes pour payer
    ELIGIBLE = "ELIGIBLE"  # l'acheteur a passé les contrôles : il peut payer
    AUTORISE = "AUTORISE"  # paiement autorisé : la somme est bloquée, pas encore débitée
    NOM_CHANGE = "NOM_CHANGE"  # l'organisateur a mis le dossard au nom de l'acheteur
    ENCAISSE = "ENCAISSE"
    VERSE = "VERSE"  # vendeur payé, commission de l'organisateur versée
    # Fins sans vente. Le dossard, lui, repart en vente dans un nouveau transfert.
    RESERVATION_EXPIREE = "RESERVATION_EXPIREE"  # l'acheteur n'a pas payé à temps
    INELIGIBLE = "INELIGIBLE"  # l'acheteur n'a pas passé les contrôles : aucun paiement déclenché
    REFUSE = "REFUSE"  # l'organisateur a refusé le transfert : autorisation annulée, acheteur jamais débité


class TransferEvent(StrEnum):
    METTRE_EN_VENTE = "METTRE_EN_VENTE"  # le dossard vérifié devient visible des acheteurs
    RESERVER = "RESERVER"  # un acheteur a cliqué « Acheter ce dossard »
    VALIDER_ACHETEUR = "VALIDER_ACHETEUR"  # identité, âge minimum, pas déjà inscrit : tout est bon
    REFUSER_ACHETEUR = "REFUSER_ACHETEUR"  # un contrôle a échoué, avant tout paiement
    EXPIRER = "EXPIRER"  # le délai de réservation est passé sans paiement
    AUTORISER = "AUTORISER"  # l'acheteur a payé : PayPal bloque les fonds
    CHANGER_NOM = "CHANGER_NOM"  # l'organisateur a confirmé le transfert
    REFUSER = "REFUSER"  # l'organisateur a refusé le transfert : l'autorisation est annulée
    ENCAISSER = "ENCAISSER"  # l'autorisation est capturée, l'acheteur est débité
    VERSER = "VERSER"  # les Payouts sont arrivés chez le vendeur et l'organisateur


# Tout couple (état, événement) absent de cette table est interdit : aucune étape ne peut être sautée.
TRANSITIONS: dict[tuple[TransferState, TransferEvent], TransferState] = {
    (TransferState.DOSSARD_VERIFIE, TransferEvent.METTRE_EN_VENTE): TransferState.EN_VENTE,
    (TransferState.EN_VENTE, TransferEvent.RESERVER): TransferState.RESERVE,
    (TransferState.RESERVE, TransferEvent.VALIDER_ACHETEUR): TransferState.ELIGIBLE,
    (TransferState.RESERVE, TransferEvent.REFUSER_ACHETEUR): TransferState.INELIGIBLE,
    # La réservation court jusqu'au paiement : elle peut expirer avant ou après les contrôles.
    (TransferState.RESERVE, TransferEvent.EXPIRER): TransferState.RESERVATION_EXPIREE,
    (TransferState.ELIGIBLE, TransferEvent.EXPIRER): TransferState.RESERVATION_EXPIREE,
    (TransferState.ELIGIBLE, TransferEvent.AUTORISER): TransferState.AUTORISE,
    (TransferState.AUTORISE, TransferEvent.CHANGER_NOM): TransferState.NOM_CHANGE,
    (TransferState.AUTORISE, TransferEvent.REFUSER): TransferState.REFUSE,
    # L'encaissement n'est possible qu'une fois le dossard au nom de l'acheteur.
    (TransferState.NOM_CHANGE, TransferEvent.ENCAISSER): TransferState.ENCAISSE,
    (TransferState.ENCAISSE, TransferEvent.VERSER): TransferState.VERSE,
}

# Dans ces états, le transfert est terminé : rien n'en sort.
FINAL_STATES = (
    TransferState.VERSE,
    TransferState.RESERVATION_EXPIREE,
    TransferState.INELIGIBLE,
    TransferState.REFUSE,
)


class InvalidTransition(Exception):
    def __init__(self, state: TransferState, event: TransferEvent):
        super().__init__(f"Transition interdite : {event} depuis l'état {state}")
        self.state = state
        self.event = event


def next_state(state: TransferState, event: TransferEvent) -> TransferState:
    """Renvoie le nouvel état, ou lève InvalidTransition si la transition est interdite."""
    try:
        return TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition(state, event) from None
