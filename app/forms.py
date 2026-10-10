"""Lecture des saisies de formulaire : tout arrive en texte, on en fait des valeurs sûres.

Chaque fonction lève ValueError avec un message à montrer tel quel à la personne.
"""

import re

# Des euros entiers, puis au plus deux décimales après une virgule ou un point.
AMOUNT = re.compile(r"(\d{1,6})(?:[.,](\d{1,2}))?", re.ASCII)
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def parse_euros(text: str) -> int | None:
    """ "45", "45,5" ou "45.50 €" -> 4500, 4550, 4550 ; vide -> None. Aucun float : on assemble des entiers."""
    text = "".join(text.replace("€", "").split())
    if not text:
        return None
    match = AMOUNT.fullmatch(text)
    if match is None:
        raise ValueError(f"Montant illisible : « {text} » (exemple : 45 ou 45,50).")
    euros_part, cents_part = match.groups()
    return int(euros_part) * 100 + int((cents_part or "").ljust(2, "0"))


def parse_email(text: str) -> str:
    email = text.strip().lower()
    if not EMAIL.fullmatch(email):
        raise ValueError("Adresse email invalide.")
    return email


def parse_name(text: str, label: str) -> str:
    """Retire les espaces en trop. `label` nomme le champ dans le message d'erreur : « Prénom », « Nom »."""
    name = " ".join(text.split())
    if not name:
        raise ValueError(f"{label} manquant.")
    return name


def parse_bib(text: str) -> int:
    if not re.fullmatch(r"\d{1,6}", text.strip(), re.ASCII):
        raise ValueError("Numéro de dossard invalide.")
    return int(text)
