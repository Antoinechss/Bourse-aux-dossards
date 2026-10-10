"""Le script scripts/sandbox_cycle.py, déroulé en entier avec un faux PayPal (aucun appel réseau)."""

from types import SimpleNamespace

import pytest
from paypal_fakes import FakePayPal

from app.payments.client import PayPalUnreachable
from scripts import sandbox_cycle


@pytest.fixture
def paypal(monkeypatch):
    fake = FakePayPal()
    fake.install(monkeypatch)
    monkeypatch.setenv("DEMO_RUNNER_EMAILS", "vendeur@example.com, acheteur@example.com")
    monkeypatch.setattr(sandbox_cycle, "settings", SimpleNamespace(demo_organizer_email="orga@example.com"))
    monkeypatch.setattr(sandbox_cycle.time, "sleep", lambda seconds: None)
    return fake


def test_nominal_cycle_authorizes_captures_and_pays_out(paypal, capsys):
    sandbox_cycle.run(refuse=False, approval_timeout=5)

    assert paypal.operations() == ["create_order", "authorize_order", "capture_authorization", "send_payouts"]
    assert [(i.recipient_email, i.amount_cents) for i in paypal.payout_items] == [
        ("vendeur@example.com", 4500),
        ("orga@example.com", 135),
    ]
    output = capsys.readouterr().out
    assert "état du transfert : VERSE" in output
    for kind in ("AUTORISATION", "CAPTURE", "VERSEMENT_VENDEUR", "VERSEMENT_ORGANISATEUR"):
        assert kind in output
    assert "Reste à la plateforme : 2,25" in output


def test_refused_cycle_voids_and_never_captures(paypal, capsys):
    sandbox_cycle.run(refuse=True, approval_timeout=5)

    assert paypal.operations() == ["create_order", "authorize_order", "void_authorization"]
    output = capsys.readouterr().out
    assert "état du transfert : REFUSE" in output
    assert "ANNULATION" in output
    assert "CAPTURE" not in output


def test_cycle_stops_when_the_buyer_never_approves(paypal, monkeypatch):
    waiting = sandbox_cycle.orders.Order(id="ORDER-1", status="PAYER_ACTION_REQUIRED", approve_url=None)
    monkeypatch.setattr(sandbox_cycle.orders, "get_order", lambda order_id: waiting)

    with pytest.raises(SystemExit):
        sandbox_cycle.run(refuse=False, approval_timeout=0)

    assert paypal.operations() == ["create_order"]


def test_a_network_cut_while_waiting_does_not_stop_the_cycle(paypal, monkeypatch):
    answers = iter([PayPalUnreachable("coupure"), PayPalUnreachable("coupure")])

    def get_order(order_id):
        # Deux coupures réseau, puis PayPal répond que l'acheteur a approuvé.
        error = next(answers, None)
        if error:
            raise error
        return sandbox_cycle.orders.Order(id=order_id, status="APPROVED", approve_url=None)

    monkeypatch.setattr(sandbox_cycle.orders, "get_order", get_order)

    sandbox_cycle.run(refuse=True, approval_timeout=60)

    assert "void_authorization" in paypal.operations()


def test_a_step_is_replayed_when_paypal_does_not_answer(paypal):
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise PayPalUnreachable("coupure")
        return "ok"

    assert sandbox_cycle.retry(flaky) == "ok"
    assert len(attempts) == 3


def test_missing_demo_emails_stop_the_script_before_any_paypal_call(paypal, monkeypatch):
    monkeypatch.setenv("DEMO_RUNNER_EMAILS", "")

    with pytest.raises(SystemExit):
        sandbox_cycle.run(refuse=False, approval_timeout=5)

    assert paypal.calls == []
