"""Moteur de templates Jinja, partagé par toutes les pages."""

from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.config import settings


def euros(cents: int) -> str:
    """4500 -> "45,00"."""
    return f"{cents // 100},{cents % 100:02d}"


def local_time(value: datetime) -> str:
    """La date et l'heure de Paris : "12/10/2026 à 14:30"."""
    if value.tzinfo is None:
        # Selon la base, la date peut revenir sans fuseau : elle est toujours en UTC.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo("Europe/Paris")).strftime("%d/%m/%Y à %H:%M")


DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def long_date(value: date) -> str:
    """La date en toutes lettres : "dimanche 23 mai 2027"."""
    day = "1er" if value.day == 1 else str(value.day)
    return f"{DAYS[value.weekday()]} {day} {MONTHS[value.month - 1]} {value.year}"


templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
# Disponible dans tous les templates (pied de page), sans avoir à le passer à chaque page.
templates.env.globals["app_env"] = settings.app_env
templates.env.globals["ai_mode"] = settings.ai_mode
templates.env.filters["euros"] = euros
templates.env.filters["local_time"] = local_time
templates.env.filters["long_date"] = long_date
