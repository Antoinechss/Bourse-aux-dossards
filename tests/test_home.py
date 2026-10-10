def test_home_page_lists_the_races(client, race):
    response = client.get("/")

    assert response.status_code == 200
    assert "bourse aux dossards" in response.text.lower()
    assert race.name in response.text
    assert f"/runner/races/{race.id}/sell" in response.text
