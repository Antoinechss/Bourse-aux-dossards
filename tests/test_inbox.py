from app.notifications import notify


def test_inbox_is_empty_at_first(client):
    assert "Aucun message pour l'instant." in client.get("/inbox").text


def test_inbox_lists_messages_newest_first(client, session):
    notify(session, "Marie@Example.com", "Premier message", "Bonjour Marie.", link="/runner/transfers/1")
    notify(session, "jean@example.com", "Second message", "Bonjour Jean.")
    session.commit()

    page = client.get("/inbox").text

    assert page.index("Second message") < page.index("Premier message")
    assert "À marie@example.com" in page
    assert 'href="/runner/transfers/1"' in page


def test_inbox_can_show_a_single_recipient(client, session):
    notify(session, "marie@example.com", "Pour Marie", "Bonjour Marie.")
    notify(session, "jean@example.com", "Pour Jean", "Bonjour Jean.")
    session.commit()

    page = client.get("/inbox", params={"email": "Marie@example.com"}).text

    assert "Pour Marie" in page
    assert "Pour Jean" not in page
