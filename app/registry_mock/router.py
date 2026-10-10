"""Endpoints du registre simulé, comme si on appelait l'API de l'organisateur."""

from typing import Annotated

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, StringConstraints

from app.db import SessionDep
from app.models import Race, Registration
from app.registry_mock.registry import change_name, list_registrations

router = APIRouter(prefix="/registry", tags=["registry_mock"])

# Un nom ne peut pas être vide, ni fait uniquement d'espaces.
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class NameChange(BaseModel):
    first_name: Name
    last_name: Name


@router.get("/races/{race_id}/registrations", response_model=list[Registration])
def get_registrations(race_id: int, session: SessionDep):
    if session.get(Race, race_id) is None:
        raise HTTPException(status_code=404, detail="Course inconnue")
    return list_registrations(session, race_id)


@router.patch("/races/{race_id}/registrations/{bib_number}", response_model=Registration)
def patch_registration_name(race_id: int, bib_number: int, body: NameChange, session: SessionDep):
    registration = change_name(session, race_id, bib_number, body.first_name, body.last_name)
    if registration is None:
        raise HTTPException(status_code=404, detail="Dossard inconnu pour cette course")
    return registration
