"""Tests qui appellent la vraie API Anthropic sur les fichiers de seed/. Ils coûtent quelques centimes.

Exclus de `uv run pytest`. Pour les lancer, avec ANTHROPIC_API_KEY renseigné dans .env :
    uv run pytest -m live
"""

from datetime import date
from pathlib import Path

import pytest

from app.ai.rules_reader import RULE_QUOTES, read_rules
from app.ai.verifier import DocumentType, NameMatch, assess, compare_names, read_proof
from app.models import ProofType, Verdict

pytestmark = pytest.mark.live

SEED = Path(__file__).parent.parent / "seed"
BOTH = {ProofType.PPS, ProofType.LICENCE_FFA}

# Pour chaque règlement : la valeur attendue de chaque règle et un passage que sa citation doit contenir.
# None : le règlement n'en parle pas, l'IA ne doit rien inventer.
EXPECTED_RULES = {
    "trail-cretes-valombre": {
        "transfer_allowed": (True, "bourse officielle"),
        "transfer_deadline": (date(2027, 3, 28), "28 mars 2027"),
        "max_resale_price_cents": (4500, "45 €"),
        "transfer_fee_cents": (500, "5 €"),
        "required_proofs": (BOTH, "PPS"),
    },
    # Piège : la clôture des inscriptions (16 mai) n'est pas une date limite de transfert.
    "10-km-port-clair": {
        "transfer_allowed": (True, "céder son dossard"),
        "transfer_deadline": None,
        "max_resale_price_cents": (2200, "22 €"),
        "transfer_fee_cents": None,
        "required_proofs": (BOTH, "PPS"),
    },
    # Pièges : les tarifs (28 € puis 34 €) ne sont pas un plafond ; « gratuite » vaut 0, pas « non précisé ».
    "semi-marathon-montbrisac": {
        "transfer_allowed": (True, "changement de titulaire"),
        "transfer_deadline": (date(2027, 3, 7), "7 mars 2027"),
        "max_resale_price_cents": None,
        "transfer_fee_cents": (0, "gratuite"),
        "required_proofs": (BOTH, "PPS"),
    },
    # Le règlement qui interdit le transfert. Piège : l'assurance annulation à 6 € n'est pas un frais de transfert.
    "marathon-deux-rives": {
        "transfer_allowed": (False, "interdit"),
        "transfer_deadline": None,
        "max_resale_price_cents": None,
        "transfer_fee_cents": None,
        "required_proofs": (BOTH, "PPS"),
    },
    # Le règlement muet sur la revente. Pièges : date limite et frais de remboursement, tarifs.
    "nocturne-pierrelune": {
        "transfer_allowed": None,
        "transfer_deadline": None,
        "max_resale_price_cents": None,
        "transfer_fee_cents": None,
        "required_proofs": None,
    },
}

# Pour chaque justificatif : ce que l'IA doit y lire, puis le verdict pour ce titulaire sur une course du 11/04/2027.
EXPECTED_PROOFS = {
    "licence-ffa-lea-moreau.png": (DocumentType.LICENCE_FFA, "Léa Moreau", date(2027, 8, 31), Verdict.VERT),
    "pps-karim-benali.pdf": (DocumentType.PPS, "Karim Benali", date(2027, 5, 12), Verdict.VERT),
    "pps-expire-hugo-lemaire.jpg": (DocumentType.PPS, "Hugo Lemaire", date(2027, 1, 5), Verdict.ROUGE),
    "carte-salle-de-sport.png": (DocumentType.AUTRE, "Léa Moreau", None, Verdict.ROUGE),
}


@pytest.mark.parametrize("name", EXPECTED_RULES)
def test_rules_reader_on_a_seed_reglement(name):
    rules = read_rules((SEED / "reglements" / f"{name}.pdf").read_bytes())

    for rule, expected in EXPECTED_RULES[name].items():
        value, quote = getattr(rules, rule), getattr(rules, RULE_QUOTES[rule])
        if expected is None:
            assert value is None, f"{rule} : inventé ({value!r}, « {quote} »)"
            continue
        expected_value, passage = expected
        assert (set(value) if isinstance(value, list) else value) == expected_value, rule
        assert quote.startswith("Article"), f"{rule} : la citation ne donne pas son article (« {quote} »)"
        assert passage in quote, f"{rule} : citation inattendue (« {quote} »)"


@pytest.mark.parametrize("name", EXPECTED_PROOFS)
def test_verifier_on_a_seed_proof(name):
    document_type, holder, valid_until, verdict = EXPECTED_PROOFS[name]

    reading = read_proof((SEED / "justificatifs" / name).read_bytes())

    assert reading.document_type is document_type
    if document_type is not DocumentType.AUTRE:
        assert compare_names(reading.holder_name, holder) is NameMatch.EXACT
        assert reading.valid_until == valid_until
        assert reading.concern is None
    assert assess(reading, buyer_name=holder, race_date=date(2027, 4, 11)).verdict is verdict
