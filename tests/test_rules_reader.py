import base64
from datetime import date

import anthropic
import httpx2
import pytest
from fakes import FakeClaude

from app.ai.client import MODEL, AIError, detect_media_type
from app.ai.rules_reader import RULE_QUOTES, clean, read_rules
from app.models import ProofType, RaceRules

PDF_BYTES = b"%PDF-1.4 faux reglement"


def rules(**values) -> RaceRules:
    """Des règles toutes vides, sauf celles passées en argument."""
    empty = {field: None for pair in RULE_QUOTES.items() for field in pair}
    return RaceRules(**{**empty, **values})


def api_error(error_class, status: int, message: str = "erreur simulée"):
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    return error_class(message, response=response, body=None)


def test_read_rules_sends_the_pdf_and_returns_the_rules():
    found = rules(
        transfer_allowed=True,
        transfer_allowed_quote="Article 9 — Le transfert est autorisé.",
        transfer_deadline=date(2027, 3, 28),
        transfer_deadline_quote="Article 9 — Aucun transfert après le 28 mars 2027.",
        required_proofs=[ProofType.PPS],
        required_proofs_quote="Article 5 — Un PPS est exigé.",
    )
    claude = FakeClaude(parsed=found)

    result = read_rules(PDF_BYTES, client=claude)

    assert result == found
    request = claude.calls[0]
    assert request["model"] == MODEL
    assert request["output_format"] is RaceRules
    assert claude.sent_file["type"] == "document"
    assert claude.sent_file["source"]["media_type"] == "application/pdf"
    assert base64.standard_b64decode(claude.sent_file["source"]["data"]) == PDF_BYTES


def test_a_rule_without_quote_is_erased():
    # L'IA affirme une règle sans pouvoir la citer : on ne la garde pas.
    claude = FakeClaude(parsed=rules(max_resale_price_cents=4500, transfer_allowed=False, transfer_allowed_quote="  "))

    result = read_rules(PDF_BYTES, client=claude)

    assert result.max_resale_price_cents is None
    assert result.transfer_allowed is None
    assert result.transfer_allowed_quote is None


def test_a_quote_without_rule_is_erased():
    claude = FakeClaude(parsed=rules(transfer_fee_quote="Article 3 — Tarif : 30 €.", required_proofs=[]))

    result = read_rules(PDF_BYTES, client=claude)

    assert result.transfer_fee_quote is None
    assert result.required_proofs is None


def test_a_free_transfer_is_kept_as_zero():
    # 0 € de frais est une vraie règle, à ne pas confondre avec « non précisé ».
    claude = FakeClaude(parsed=rules(transfer_fee_cents=0, transfer_fee_quote="Article 11 — Cette opération est gratuite."))

    assert read_rules(PDF_BYTES, client=claude).transfer_fee_cents == 0


def test_clean_keeps_a_rule_typed_by_the_organizer_without_quote():
    result = clean(rules(max_resale_price_cents=3000), require_quote=False)

    assert result.max_resale_price_cents == 3000
    assert result.max_resale_price_quote is None


def test_read_rules_refuses_anything_but_a_pdf():
    claude = FakeClaude()

    with pytest.raises(AIError, match="PDF"):
        read_rules(b"\x89PNG\r\n\x1a\n une image", client=claude)

    assert claude.calls == []


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_an_unfinished_answer_is_an_error(stop_reason):
    claude = FakeClaude(parsed=None, stop_reason=stop_reason)

    with pytest.raises(AIError):
        read_rules(PDF_BYTES, client=claude)


@pytest.mark.parametrize(
    "error, expected",
    [
        (api_error(anthropic.AuthenticationError, 401), "Clé API"),
        (api_error(anthropic.BadRequestError, 400), "illisible"),
        # Le texte exact renvoyé par l'API quand le compte n'a pas de crédit.
        (api_error(anthropic.BadRequestError, 400, "Your credit balance is too low to access the Anthropic API."), "plus de crédit"),
        (api_error(anthropic.RateLimitError, 429), "Trop de demandes"),
        (api_error(anthropic.InternalServerError, 500), "erreur"),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")), "injoignable"),
    ],
)
def test_api_errors_become_readable_messages(error, expected):
    with pytest.raises(AIError, match=expected):
        read_rules(PDF_BYTES, client=FakeClaude(error=error))


def test_missing_api_key_is_reported_before_any_call(monkeypatch):
    monkeypatch.setattr("app.ai.client.settings", type("S", (), {"anthropic_api_key": ""})())

    with pytest.raises(AIError, match="ANTHROPIC_API_KEY"):
        read_rules(PDF_BYTES)


@pytest.mark.parametrize(
    "data, expected",
    [
        (b"%PDF-1.7 ...", "application/pdf"),
        (b"\xff\xd8\xff\xe0 ...", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n ...", "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"GIF89a ...", "image/gif"),
        (b"PK\x03\x04 un docx", None),
        (b"", None),
    ],
)
def test_detect_media_type(data, expected):
    assert detect_media_type(data) == expected
