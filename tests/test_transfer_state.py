from itertools import product

import pytest

from app.transfers.state import FINAL_STATES, InvalidTransition, TransferEvent, TransferState, next_state

S = TransferState
E = TransferEvent

# Écrit à la main, sans importer la table du code : si elle est modifiée par erreur, ces tests échouent.
ALLOWED = [
    (S.DOSSARD_VERIFIE, E.METTRE_EN_VENTE, S.EN_VENTE),
    (S.EN_VENTE, E.RESERVER, S.RESERVE),
    (S.RESERVE, E.VALIDER_ACHETEUR, S.ELIGIBLE),
    (S.RESERVE, E.REFUSER_ACHETEUR, S.INELIGIBLE),
    (S.RESERVE, E.EXPIRER, S.RESERVATION_EXPIREE),
    (S.ELIGIBLE, E.EXPIRER, S.RESERVATION_EXPIREE),
    (S.ELIGIBLE, E.AUTORISER, S.AUTORISE),
    (S.AUTORISE, E.CHANGER_NOM, S.NOM_CHANGE),
    (S.AUTORISE, E.REFUSER, S.REFUSE),
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


# --- Les garde-fous que le nouvel ordre doit tenir, dits en clair ---


def test_a_bib_is_never_on_sale_before_it_is_verified():
    # Le seul chemin vers EN_VENTE part d'un dossard vérifié.
    assert [state for state, _, target in ALLOWED if target is S.EN_VENTE] == [S.DOSSARD_VERIFIE]


def test_payment_can_only_be_authorized_for_an_eligible_buyer():
    assert [state for state, event, _ in ALLOWED if event is E.AUTORISER] == [S.ELIGIBLE]


def test_payment_can_only_be_captured_once_the_bib_is_in_the_buyer_s_name():
    assert [state for state, event, _ in ALLOWED if event is E.ENCAISSER] == [S.NOM_CHANGE]


@pytest.mark.parametrize("state", [S.DOSSARD_VERIFIE, S.EN_VENTE, S.RESERVE, S.ELIGIBLE, S.AUTORISE])
def test_no_step_can_be_skipped_to_reach_capture_or_payout(state):
    for event in (E.ENCAISSER, E.VERSER):
        with pytest.raises(InvalidTransition):
            next_state(state, event)


@pytest.mark.parametrize("state", FINAL_STATES)
def test_nothing_leaves_a_final_state(state):
    for event in E:
        with pytest.raises(InvalidTransition):
            next_state(state, event)


def test_every_state_is_reachable_and_only_final_states_are_dead_ends():
    reachable = {S.DOSSARD_VERIFIE} | {target for _, _, target in ALLOWED}
    assert reachable == set(S)
    assert {state for state in S if not any(source is state for source, _, _ in ALLOWED)} == set(FINAL_STATES)
