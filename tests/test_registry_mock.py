from datetime import date

import pytest

from app.models import Race, Registration


@pytest.fixture
def registrations(session, race):
    # Insérés dans le désordre pour vérifier le tri par numéro de dossard.
    session.add_all(
        [
            Registration(race_id=race.id, bib_number=42, first_name="Jean", last_name="Dupont"),
            Registration(race_id=race.id, bib_number=7, first_name="Marie", last_name="Martin"),
        ]
    )
    session.commit()


def test_list_registrations_sorted_by_bib_number(client, race, registrations):
    response = client.get(f"/registry/races/{race.id}/registrations")

    assert response.status_code == 200
    assert [(r["bib_number"], r["first_name"], r["last_name"]) for r in response.json()] == [
        (7, "Marie", "Martin"),
        (42, "Jean", "Dupont"),
    ]


def test_list_registrations_only_returns_the_requested_race(client, session, race, registrations):
    other = Race(name="10 km du Port", event_date=date(2027, 6, 6), organizer_email="port@example.com")
    session.add(other)
    session.commit()
    session.add(Registration(race_id=other.id, bib_number=7, first_name="Paul", last_name="Durand"))
    session.commit()

    response = client.get(f"/registry/races/{other.id}/registrations")

    assert [r["last_name"] for r in response.json()] == ["Durand"]


def test_list_registrations_of_unknown_race_is_404(client):
    assert client.get("/registry/races/999/registrations").status_code == 404


def test_change_name_updates_the_bib(client, session, race, registrations):
    response = client.patch(
        f"/registry/races/{race.id}/registrations/42",
        json={"first_name": "Lucie", "last_name": "Bernard"},
    )

    assert response.status_code == 200
    assert response.json()["first_name"] == "Lucie"
    assert response.json()["last_name"] == "Bernard"
    # Le changement est bien enregistré, et l'autre dossard n'a pas bougé.
    names = {r["bib_number"]: r["last_name"] for r in client.get(f"/registry/races/{race.id}/registrations").json()}
    assert names == {7: "Martin", 42: "Bernard"}


def test_change_name_of_unknown_bib_is_404(client, race, registrations):
    response = client.patch(
        f"/registry/races/{race.id}/registrations/999",
        json={"first_name": "Lucie", "last_name": "Bernard"},
    )

    assert response.status_code == 404


def test_change_name_rejects_blank_name(client, session, race, registrations):
    response = client.patch(
        f"/registry/races/{race.id}/registrations/42",
        json={"first_name": "   ", "last_name": "Bernard"},
    )

    assert response.status_code == 422
    assert client.get(f"/registry/races/{race.id}/registrations").json()[1]["first_name"] == "Jean"
