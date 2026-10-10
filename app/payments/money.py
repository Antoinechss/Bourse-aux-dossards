"""Conversion entre nos centimes entiers et les montants PayPal, qui sont des chaînes ("48.60")."""

from decimal import Decimal

# Toute la démo est en euros.
CURRENCY = "EUR"


def cents_to_paypal(cents: int) -> str:
    """4860 -> "48.60"."""
    if not isinstance(cents, int):
        raise TypeError("Un montant est un nombre entier de centimes, jamais un float")
    if cents < 0:
        raise ValueError("Un montant ne peut pas être négatif")
    return f"{cents // 100}.{cents % 100:02d}"


def format_euros(cents: int) -> str:
    """4860 -> "48,60 €", pour l'affichage. L'espace est insécable : le symbole ne passe pas à la ligne."""
    return f"{cents // 100},{cents % 100:02d} €"


def paypal_to_cents(value: str) -> int:
    """ "48.60" -> 4860. Decimal est exact, contrairement à float."""
    cents = Decimal(value) * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"Montant PayPal avec plus de deux décimales : {value}")
    return int(cents)
