"""Connexion à la base : moteur, création des tables, session par requête."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from sqlmodel import Session, SQLModel, create_engine

from app import models  # noqa: F401  (enregistre les tables auprès de SQLModel)
from app.config import settings

# SQLite refuse par défaut qu'une connexion change de thread, ce que fait FastAPI.
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
