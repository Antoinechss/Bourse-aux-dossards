from datetime import date, timedelta

import pytest
from fakes import FakeClaude

from app.ai.verifier import (
    AccountSignals,
    DocumentType,
    NameMatch,
    ProofReading,
    account_signals,
    assess,
    compare_names,
    read_proof,
)
from app.models import Listing, ProofType, RaceRulesRow, Verdict, WaitlistEntry, utcnow

RACE_DAY = date(2027, 4, 11)
RESELLER = AccountSignals(listing_count=4, at_cap_count=3, is_recent=True)


def reading(**changes) -> ProofReading:
    """Une licence en règle au nom de Léa Moreau, sauf ce qui est passé en argument."""
    values = {
        "document_type": DocumentType.LICENCE_FFA,
        "holder_name": "MOREAU Léa",
        "valid_until": date(2027, 8, 31),
        "concern": None,
    }
    return ProofReading(**{**values, **changes})


def assess_for_lea(proof: ProofReading, **context):
    return assess(proof, buyer_name="Léa Moreau", race_date=RACE_DAY, **context)


# --- Le verdict ---


def test_matching_valid_proof_is_green():
    result = assess_for_lea(reading())

    assert result.verdict is Verdict.VERT
    assert result.proof_type is ProofType.LICENCE_FFA
    assert result.explanation == "Licence FFA au nom de MOREAU Léa, valide jusqu'au 31/08/2027 : tout concorde."


def test_proof_expiring_on_race_day_is_still_valid():
    assert assess_for_lea(reading(valid_until=RACE_DAY)).verdict is Verdict.VERT


def test_proof_expiring_before_the_race_is_red():
    result = assess_for_lea(reading(valid_until=RACE_DAY - timedelta(days=1)))

    assert result.verdict is Verdict.ROUGE
    assert result.explanation == "Le justificatif expire le 10/04/2027, avant la course du 11/04/2027."


def test_another_document_is_red_with_a_single_reason():
    result = assess_for_lea(reading(document_type=DocumentType.AUTRE, holder_name="Quelqu'un d'autre", valid_until=None))

    assert result.verdict is Verdict.ROUGE
    assert result.proof_type is None
    assert result.explanation == "Le document n'est ni une licence FFA ni un PPS."


def test_someone_else_s_proof_is_red():
    result = assess_for_lea(reading(holder_name="Karim BENALI"))

    assert result.verdict is Verdict.ROUGE
    assert result.explanation == "Le justificatif est au nom de Karim BENALI, et non de Léa Moreau."


def test_partially_matching_name_is_orange():
    result = assess(reading(), buyer_name="Léa Moreau-Dupuis", race_date=RACE_DAY)

    assert result.verdict is Verdict.ORANGE
    assert "ne correspond qu'en partie" in result.explanation


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"holder_name": None}, "Le nom du titulaire est illisible."),
        ({"valid_until": None}, "La date de fin de validité est illisible."),
        ({"concern": "Le bas du document est coupé."}, "Le bas du document est coupé."),
    ],
)
def test_what_cannot_be_read_is_orange(changes, expected):
    result = assess_for_lea(reading(**changes))

    assert result.verdict is Verdict.ORANGE
    assert result.explanation == expected


def test_proof_not_accepted_by_the_race_rules_is_red():
    result = assess_for_lea(reading(), accepted_proofs=[ProofType.PPS])

    assert result.verdict is Verdict.ROUGE
    assert "n'accepte pas ce justificatif (Licence FFA)" in result.explanation


@pytest.mark.parametrize("accepted", [None, [], [ProofType.PPS, ProofType.LICENCE_FFA], ["LICENCE_FFA"]])
def test_proof_accepted_or_rules_silent_is_green(accepted):
    assert assess_for_lea(reading(), accepted_proofs=accepted).verdict is Verdict.VERT


def test_reseller_signals_on_the_seller_make_a_perfect_proof_orange():
    result = assess_for_lea(reading(), seller=RESELLER)

    assert result.verdict is Verdict.ORANGE
    assert result.explanation == "Le compte vendeur a déposé 4 annonces, dont 3 au prix plafond, et il est récent."


def test_reseller_signals_on_the_buyer_make_a_perfect_proof_orange():
    result = assess_for_lea(reading(), buyer=AccountSignals(listing_count=3))

    assert result.verdict is Verdict.ORANGE
    assert result.explanation == "Le compte acheteur a déposé 3 annonces."


def test_all_reasons_fit_in_one_sentence_with_the_blocking_ones_first():
    result = assess_for_lea(reading(holder_name=None, valid_until=date(2027, 1, 5)), seller=RESELLER)

    assert result.verdict is Verdict.ROUGE
    assert result.explanation == (
        "Le justificatif expire le 05/01/2027, avant la course du 11/04/2027 ; le nom du titulaire est illisible ; "
        "le compte vendeur a déposé 4 annonces, dont 3 au prix plafond, et il est récent."
    )


# --- La comparaison des noms ---


@pytest.mark.parametrize(
    "document_name, buyer_name, expected",
    [
        ("MOREAU Léa", "Léa Moreau", NameMatch.EXACT),
        ("Lea MOREAU", "Léa Moreau", NameMatch.EXACT),
        ("Jean-Pierre DUPONT", "dupont jean pierre", NameMatch.EXACT),
        ("D'ARTAGNAN Charles", "Charles d’Artagnan", NameMatch.EXACT),
        ("COEUR Anne", "Anne Cœur", NameMatch.EXACT),
        ("Léa MOREAU", "Léa Moreau-Dupuis", NameMatch.PARTIAL),
        ("DUPONT", "Jean Dupont", NameMatch.PARTIAL),
        ("Marie DUPONT", "Jean Dupont", NameMatch.NONE),
        ("Léa MOREAU", "Karim Benali", NameMatch.NONE),
        ("", "Léa Moreau", NameMatch.NONE),
    ],
)
def test_compare_names(document_name, buyer_name, expected):
    assert compare_names(document_name, buyer_name) is expected


# --- Les signaux anti-revendeur ---


@pytest.mark.parametrize(
    "listings, at_cap, recent, suspicious",
    [
        (0, 0, True, False),  # un acheteur qui n'a jamais rien vendu
        (1, 1, True, False),  # un vendeur honnête : un dossard, au prix d'achat, compte neuf
        (2, 2, False, False),
        (2, 1, True, False),
        (2, 2, True, True),  # deux annonces au plafond depuis un compte tout neuf
        (3, 0, False, True),  # trois annonces, quel que soit le reste
    ],
)
def test_account_is_suspicious(listings, at_cap, recent, suspicious):
    assert AccountSignals(listing_count=listings, at_cap_count=at_cap, is_recent=recent).suspicious is suspicious


def test_account_signals_count_listings_and_those_at_the_price_cap(session, race):
    session.add(RaceRulesRow(race_id=race.id, max_resale_price_cents=4500))
    session.add_all(
        [
            Listing(race_id=race.id, bib_number=1, seller_email="Vendeur@Example.com", price_cents=4500),
            Listing(race_id=race.id, bib_number=2, seller_email="vendeur@example.com", price_cents=4000),
            Listing(race_id=race.id, bib_number=3, seller_email="autre@example.com", price_cents=4500),
        ]
    )
    session.commit()

    signals = account_signals(session, "vendeur@example.com")

    assert signals == AccountSignals(listing_count=2, at_cap_count=1, is_recent=True)


def test_no_listing_is_at_the_cap_when_the_race_has_none(session, race):
    session.add(Listing(race_id=race.id, bib_number=1, seller_email="vendeur@example.com", price_cents=9900))
    session.commit()

    assert account_signals(session, "vendeur@example.com").at_cap_count == 0


def test_account_is_recent_only_if_it_was_first_seen_this_week(session, race):
    now = utcnow()
    long_ago, yesterday = now - timedelta(days=30), now - timedelta(days=1)
    session.add_all(
        [
            # Ancien compte : inscrit en liste d'attente il y a un mois, vend un dossard aujourd'hui.
            WaitlistEntry(race_id=race.id, first_name="A", last_name="B", email="ancien@example.com", created_at=long_ago),
            Listing(race_id=race.id, bib_number=1, seller_email="ancien@example.com", price_cents=4000, created_at=yesterday),
            Listing(race_id=race.id, bib_number=2, seller_email="nouveau@example.com", price_cents=4000, created_at=yesterday),
        ]
    )
    session.commit()

    assert account_signals(session, "ancien@example.com", now=now).is_recent is False
    assert account_signals(session, "nouveau@example.com", now=now).is_recent is True
    assert account_signals(session, "inconnu@example.com", now=now) == AccountSignals()


# --- La lecture par l'IA ---


def test_read_proof_sends_an_image_and_never_the_buyer_identity():
    found = reading()
    claude = FakeClaude(parsed=found)

    result = read_proof(b"\x89PNG\r\n\x1a\n une image", client=claude)

    assert result == found
    assert claude.sent_file["type"] == "image"
    assert claude.sent_file["source"]["media_type"] == "image/png"
    assert claude.calls[0]["output_format"] is ProofReading


def test_read_proof_sends_a_pdf_as_a_document():
    claude = FakeClaude(parsed=reading())

    read_proof(b"%PDF-1.4 une attestation", client=claude)

    assert claude.sent_file["type"] == "document"
