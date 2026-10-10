"""Calcul des montants d'une vente : fonction pure, en centimes entiers.

L'acheteur paie le prix du dossard plus les frais de service. Le vendeur touche le prix en
entier, l'organisateur sa part des frais, la plateforme garde le reste.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Amounts:
    price_cents: int  # prix du dossard, reversé en entier au vendeur
    platform_fee_cents: int  # part des frais gardée par la plateforme
    organizer_fee_cents: int  # part des frais reversée à l'organisateur

    @property
    def service_fee_cents(self) -> int:
        """Frais de service affichés à l'acheteur."""
        return self.platform_fee_cents + self.organizer_fee_cents

    @property
    def total_cents(self) -> int:
        """Ce que paie l'acheteur."""
        return self.price_cents + self.service_fee_cents


def _share_cents(price_cents: int, bps: int) -> int:
    # Arrondi au centime le plus proche, le demi-centime vers le haut, sans passer par un float.
    return (price_cents * bps + 5_000) // 10_000


def compute_amounts(price_cents: int, platform_fee_bps: int, organizer_fee_bps: int) -> Amounts:
    """Répartit une vente. Les taux sont en points de base (500 = 5 %).

    On arrondit d'abord le total des frais, pour que l'acheteur paie exactement le taux
    affiché (8 % par défaut). L'organisateur touche sa part arrondie, la plateforme le reste :
    les trois montants retombent ainsi toujours sur le total, au centime près.
    """
    for value in (price_cents, platform_fee_bps, organizer_fee_bps):
        if not isinstance(value, int):
            raise TypeError("Montants et taux sont des entiers (centimes, points de base), jamais des floats")
    if price_cents <= 0:
        raise ValueError("Le prix doit être strictement positif")
    if platform_fee_bps < 0 or organizer_fee_bps < 0:
        raise ValueError("Un taux de commission ne peut pas être négatif")

    service_fee_cents = _share_cents(price_cents, platform_fee_bps + organizer_fee_bps)
    organizer_fee_cents = _share_cents(price_cents, organizer_fee_bps)
    return Amounts(
        price_cents=price_cents,
        platform_fee_cents=service_fee_cents - organizer_fee_cents,
        organizer_fee_cents=organizer_fee_cents,
    )
