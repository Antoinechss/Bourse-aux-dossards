"""Appel à Claude commun aux deux lecteurs : on envoie un fichier, on reçoit un objet Pydantic."""

import base64
import logging

import anthropic
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5-5"

PDF = "application/pdf"
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp", "image/gif")


class AIError(Exception):
    """L'analyse n'a pas abouti. Le message est en français et peut être montré tel quel à l'utilisateur."""


def detect_media_type(data: bytes) -> str | None:
    """Reconnaît un PDF ou une image à ses premiers octets, sans se fier au nom du fichier."""
    if data.startswith(b"%PDF-"):
        return PDF
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None


def get_client() -> anthropic.Anthropic:
    if not settings.anthropic_api_key:
        raise AIError("ANTHROPIC_API_KEY est vide : renseigne-le dans .env.")
    # Le SDK lit lui-même ANTHROPIC_API_KEY dans l'environnement, chargé depuis .env.
    return anthropic.Anthropic(timeout=120.0)


def extract(
    *,
    data: bytes,
    system: str,
    instruction: str,
    output_format: type[BaseModel],
    client: anthropic.Anthropic | None = None,
) -> BaseModel:
    """Envoie le fichier à Claude et renvoie sa réponse sous la forme d'un objet `output_format`."""
    media_type = detect_media_type(data)
    source = {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(data).decode("ascii")}
    if media_type == PDF:
        file_block = {"type": "document", "source": source}
    elif media_type in IMAGE_TYPES:
        file_block = {"type": "image", "source": source}
    else:
        raise AIError("Format non reconnu : envoie un PDF ou une image (JPEG, PNG, WebP, GIF).")

    client = client or get_client()
    try:
        response = client.beta.messages.parse(
            model=MODEL,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": [file_block, {"type": "text", "text": instruction}]}],
            output_format=output_format,
            output_config={"effort": "medium"},
            # Si un filtre de sécurité refuse la demande, l'API la rejoue d'elle-même sur un autre modèle.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as error:
        raise AIError("Clé API Anthropic refusée : vérifie ANTHROPIC_API_KEY dans .env.") from error
    except anthropic.BadRequestError as error:
        logger.warning("Requête refusée par l'API Anthropic : %s", error)
        raise AIError("Le fichier n'a pas pu être lu : il est illisible, protégé ou trop volumineux.") from error
    except anthropic.RateLimitError as error:
        raise AIError("Trop de demandes en cours : réessaie dans une minute.") from error
    except anthropic.APIStatusError as error:
        logger.warning("Erreur %s de l'API Anthropic : %s", error.status_code, error)
        raise AIError("Le service d'analyse a rencontré une erreur : réessaie dans un instant.") from error
    except anthropic.APIConnectionError as error:
        raise AIError("Le service d'analyse est injoignable : vérifie la connexion et réessaie.") from error

    if response.stop_reason == "refusal":
        raise AIError("L'IA a refusé d'analyser ce document.")
    if response.stop_reason == "max_tokens" or response.parsed_output is None:
        raise AIError("La réponse de l'IA est incomplète : réessaie.")
    return response.parsed_output
