"""Machine à états d'un transfert : fonction pure, sans base de données ni PayPal."""

from enum import StrEnum


class TransferState(StrEnum):
    EN_VENTE = "EN_VENTE"
    RESERVE = "RESERVE"
    AUTORISE = "AUTORISE"
    VERIFIE = "VERIFIE"
    NOM_CHANGE = "NOM_CHANGE"
    ENCAISSE = "ENCAISSE"
    VERSE = "VERSE"
    REFUSE = "REFUSE"
    OFFRE_EXPIREE = "OFFRE_EXPIREE"


class TransferEvent(StrEnum):
    RESERVER = "RESERVER"  # le dossard est proposé au suivant de la liste d'attente
    AUTORISER = "AUTORISER"  # l'acheteur a payé : PayPal bloque les fonds
    EXPIRER = "EXPIRER"  # l'acheteur n'a pas payé dans le délai de l'offre
    VERIFIER = "VERIFIER"  # justificatif accepté (IA au vert, sinon l'organisateur)
    REFUSER = "REFUSER"  # justificatif refusé : l'autorisation est annulée
    CHANGER_NOM = "CHANGER_NOM"  # l'organisateur a mis le dossard au nom de l'acheteur
    ENCAISSER = "ENCAISSER"  # l'autorisation est capturée, l'acheteur est débité
    VERSER = "VERSER"  # les Payouts sont partis vers le vendeur et l'organisateur


# Tout couple (état, événement) absent de cette table est interdit.
# VERSE, REFUSE et OFFRE_EXPIREE sont des états finaux : rien n'en sort.
TRANSITIONS: dict[tuple[TransferState, TransferEvent], TransferState] = {
    (TransferState.EN_VENTE, TransferEvent.RESERVER): TransferState.RESERVE,
    (TransferState.RESERVE, TransferEvent.AUTORISER): TransferState.AUTORISE,
    (TransferState.RESERVE, TransferEvent.EXPIRER): TransferState.OFFRE_EXPIREE,
    (TransferState.AUTORISE, TransferEvent.VERIFIER): TransferState.VERIFIE,
    (TransferState.AUTORISE, TransferEvent.REFUSER): TransferState.REFUSE,
    (TransferState.VERIFIE, TransferEvent.CHANGER_NOM): TransferState.NOM_CHANGE,
    (TransferState.NOM_CHANGE, TransferEvent.ENCAISSER): TransferState.ENCAISSE,
    (TransferState.ENCAISSE, TransferEvent.VERSER): TransferState.VERSE,
}


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
