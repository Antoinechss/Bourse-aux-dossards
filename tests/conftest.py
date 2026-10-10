from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from app.db import get_session
from app.main import app
from app.models import Race


@pytest.fixture
def session():
    # Base SQLite en mémoire, neuve pour chaque test.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


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
