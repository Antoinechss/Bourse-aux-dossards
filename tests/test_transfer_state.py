from itertools import product

import pytest

from app.transfers.state import InvalidTransition, TransferEvent, TransferState, next_state

S = TransferState
E = TransferEvent

# Écrit à la main, sans importer la table du code : si elle est modifiée par erreur, ces tests échouent.
ALLOWED = [
    (S.EN_VENTE, E.RESERVER, S.RESERVE),
    (S.RESERVE, E.AUTORISER, S.AUTORISE),
    (S.RESERVE, E.EXPIRER, S.OFFRE_EXPIREE),
    (S.AUTORISE, E.VERIFIER, S.VERIFIE),
    (S.AUTORISE, E.REFUSER, S.REFUSE),
    (S.VERIFIE, E.CHANGER_NOM, S.NOM_CHANGE),
    (S.NOM_CHANGE, E.ENCAISSER, S.ENCAISSE),
    (S.ENCAISSE, E.VERSER, S.VERSE),
]

# Tous les autres couples (état, événement) possibles.
ALLOWED_PAIRS = {(state, event) for state, event, _ in ALLOWED}
FORBIDDEN = [pair for pair in product(S, E) if pair not in ALLOWED_PAIRS]


@pytest.mark.parametrize("state, event, expected", ALLOWED)
def test_allowed_transition(state, event, expected):
    assert next_state(state, event) == expected


@pytest.mark.parametrize("state, event", FORBIDDEN)
def test_forbidden_transition(state, event):
    with pytest.raises(InvalidTransition):
        next_state(state, event)
