import os
from datetime import date

# Les tests ne dépendent pas du .env du développeur : l'IA simulée y est toujours coupée.
# À faire avant d'importer l'application, qui lit sa configuration au chargement.
os.environ["AI_MODE"] = "real"

import pytest  # noqa: E402
from fastapi.testclient import TestClient
from sqlalchemy import make_url
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.db import get_session, make_engine
from app.main import app
from app.models import Race


def make_test_engine():
    """Par défaut, une base SQLite en mémoire. Avec TEST_DATABASE_URL, cette base-là, par exemple un Postgres.

    Elle est entièrement vidée avant chaque test : par sécurité, son nom doit contenir « test ».
    """
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        return create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    if "test" not in (make_url(url).database or ""):
        raise RuntimeError("TEST_DATABASE_URL doit désigner une base dont le nom contient « test » : elle est vidée")
    engine = make_engine(url)
    SQLModel.metadata.drop_all(engine)
    return engine


@pytest.fixture
def session():
    # Une base neuve pour chaque test.
    engine = make_test_engine()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def client(session):
    # Les routes reçoivent la session de test à la place de la vraie base.
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def race(session):
    race = Race(name="Trail des Crêtes", event_date=date(2027, 4, 11), organizer_email="orga@example.com")
    session.add(race)
    session.commit()
    session.refresh(race)
    return race
