"""Connexion à la base : moteur, création des tables, session par requête."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import Engine
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  (enregistre les tables auprès de SQLModel)
from app.config import settings


def make_engine(url: str) -> Engine:
    """Le moteur adapté à cette URL : SQLite en local, Postgres (Neon) en prod."""
    if url.startswith("sqlite"):
        # SQLite refuse par défaut qu'une connexion change de thread, ce que fait FastAPI.
        return create_engine(url, connect_args={"check_same_thread": False})
    # Les hébergeurs donnent une URL "postgres://" ou "postgresql://" : on y précise le pilote, psycopg.
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url.removeprefix(prefix)
    return create_engine(
        url,
        # Neon coupe les connexions restées inactives : chacune est testée avant de servir.
        pool_pre_ping=True,
        # Pas de requêtes préparées : le pooler de connexions de Neon ne les garde pas d'une requête à l'autre.
        connect_args={"prepare_threshold": None},
    )


engine = make_engine(settings.database_url)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
