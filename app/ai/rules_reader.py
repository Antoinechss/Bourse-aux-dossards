"""Lecteur de règlement : le PDF du règlement d'une course vers un objet RaceRules."""

import anthropic

from app.ai.client import PDF, AIError, detect_media_type, extract
from app.models import RaceRules

# Chaque règle et le champ qui porte sa citation.
RULE_QUOTES = {
    "transfer_allowed": "transfer_allowed_quote",
    "transfer_deadline": "transfer_deadline_quote",
    "max_resale_price_cents": "max_resale_price_quote",
    "transfer_fee_cents": "transfer_fee_quote",
    "required_proofs": "required_proofs_quote",
}

SYSTEM = """\
Tu lis le règlement d'une course à pied pour Relais, la bourse aux dossards officielle des organisateurs. \
L'organisateur s'appuiera sur ta lecture pour autoriser ou non la revente des dossards de sa course, \
et il la relira avant de la confirmer.

Relève uniquement ce que le règlement écrit noir sur blanc. Une règle inventée ou déduite serait appliquée \
à de vrais coureurs : si le règlement ne dit rien sur un point, la valeur et sa citation valent null. \
Ne complète pas avec ce qui se pratique habituellement sur d'autres courses.

Chaque valeur non nulle s'accompagne de la phrase du règlement qui la justifie, recopiée mot pour mot \
et précédée du numéro de son article, pour que l'organisateur puisse la retrouver dans son document."""

INSTRUCTION = "Voici le règlement de la course. Relève ce qu'il dit de la revente des dossards."


def clean(rules: RaceRules, require_quote: bool) -> RaceRules:
    """Renvoie une copie où une citation ne reste jamais sans sa règle.

    Avec require_quote, l'inverse vaut aussi : une règle sans citation est effacée,
    car rien ne prouve qu'elle vient du règlement.
    """
    changes = {}
    for rule, quote in RULE_QUOTES.items():
        value = getattr(rules, rule)
        text = (getattr(rules, quote) or "").strip() or None
        if value == []:
            value = None
        if value is None or (require_quote and text is None):
            value, text = None, None
        changes[rule], changes[quote] = value, text
    return rules.model_copy(update=changes)


def read_rules(pdf: bytes, client: anthropic.Anthropic | None = None) -> RaceRules:
    """Envoie le règlement à Claude et renvoie les règles qu'il y a trouvées, chacune avec sa citation."""
    if detect_media_type(pdf) != PDF:
        raise AIError("Le règlement doit être un fichier PDF.")
    rules = extract(data=pdf, system=SYSTEM, instruction=INSTRUCTION, output_format=RaceRules, client=client)
    return clean(rules, require_quote=True)
