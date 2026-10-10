import pytest

from app.payments.commission import Amounts, compute_amounts
from app.payments.money import cents_to_paypal, format_euros, paypal_to_cents


def test_default_commission_on_a_45_euro_bib():
    amounts = compute_amounts(4500, platform_fee_bps=500, organizer_fee_bps=300)

    assert amounts == Amounts(price_cents=4500, platform_fee_cents=225, organizer_fee_cents=135)
    assert amounts.service_fee_cents == 360
    assert amounts.total_cents == 4860


# Calculés à la main : prix, frais plateforme, frais organisateur.
@pytest.mark.parametrize(
    "price, platform, organizer",
    [
        (10000, 500, 300),  # 100,00 € : tombe juste
        (4599, 230, 138),  # 8 % = 367,92 -> 368 ; 3 % = 137,97 -> 138 ; reste 230
        (1250, 62, 38),  # 8 % = 100 ; 3 % = 37,5 -> 38 ; reste 62 (et non 63)
        (1, 0, 0),  # 0,01 € : les frais s'arrondissent à zéro
        (6, 0, 0),  # 8 % = 0,48 -> 0
        (7, 1, 0),  # 8 % = 0,56 -> 1 ; 3 % = 0,21 -> 0
    ],
)
def test_rounding_to_the_nearest_cent(price, platform, organizer):
    amounts = compute_amounts(price, platform_fee_bps=500, organizer_fee_bps=300)

    assert (amounts.platform_fee_cents, amounts.organizer_fee_cents) == (platform, organizer)


def test_buyer_pays_exactly_the_displayed_rate_and_nothing_is_lost():
    # Tous les prix de 0,01 € à 300,00 €.
    for price in range(1, 30_001):
        amounts = compute_amounts(price, platform_fee_bps=500, organizer_fee_bps=300)

        # Les frais payés sont bien 8 % du prix, arrondis au centime le plus proche.
        assert amounts.service_fee_cents == (price * 8 + 50) // 100, price
        # Ce que paie l'acheteur est entièrement réparti : vendeur + organisateur + plateforme.
        assert amounts.total_cents == price + amounts.organizer_fee_cents + amounts.platform_fee_cents, price
        assert amounts.platform_fee_cents >= 0, price


def test_commission_is_configurable_per_race():
    amounts = compute_amounts(5000, platform_fee_bps=400, organizer_fee_bps=600)

    assert (amounts.platform_fee_cents, amounts.organizer_fee_cents) == (200, 300)


def test_zero_commission_is_allowed():
    amounts = compute_amounts(5000, platform_fee_bps=0, organizer_fee_bps=0)

    assert amounts.total_cents == 5000


@pytest.mark.parametrize("price", [0, -100])
def test_price_must_be_positive(price):
    with pytest.raises(ValueError):
        compute_amounts(price, platform_fee_bps=500, organizer_fee_bps=300)


def test_negative_rate_is_rejected():
    with pytest.raises(ValueError):
        compute_amounts(4500, platform_fee_bps=-1, organizer_fee_bps=300)


def test_float_amounts_are_rejected():
    with pytest.raises(TypeError):
        compute_amounts(45.0, platform_fee_bps=500, organizer_fee_bps=300)


@pytest.mark.parametrize(
    "cents, text",
    [(4860, "48.60"), (135, "1.35"), (5, "0.05"), (0, "0.00"), (10000, "100.00"), (123456789, "1234567.89")],
)
def test_cents_to_paypal_and_back(cents, text):
    assert cents_to_paypal(cents) == text
    assert paypal_to_cents(text) == cents


def test_cents_to_paypal_rejects_floats_and_negatives():
    with pytest.raises(TypeError):
        cents_to_paypal(48.6)
    with pytest.raises(ValueError):
        cents_to_paypal(-1)


def test_format_euros_for_display():
    assert format_euros(4860) == "48,60 €"
    assert format_euros(5) == "0,05 €"


def test_paypal_to_cents_accepts_short_forms_but_not_fractions_of_a_cent():
    assert paypal_to_cents("48") == 4800
    assert paypal_to_cents("48.6") == 4860
    with pytest.raises(ValueError):
        paypal_to_cents("48.605")
