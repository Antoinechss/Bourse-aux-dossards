"""IA simulée, pour essayer l'application sans appeler Claude (AI_MODE=fake dans .env).

Elle ne lit rien. Elle reconnaît les fichiers d'exemple de seed/ à leur contenu exact et renvoie,
pour chacun, la réponse rangée dans seed/fake_ai.json. Tout autre fichier est refusé.
"""

import hashlib
import json
from functools import cache
from pathlib import Path

from app.ai.client import AIError

SEED = Path(__file__).parent.parent.parent / "seed"


@cache
def _answers(folder: str) -> dict[str, dict]:
    """Pour chaque fichier d'exemple du dossier : son empreinte, et la réponse à renvoyer."""
    answers = json.loads((SEED / "fake_ai.json").read_text(encoding="utf-8"))[folder]
    return {hashlib.sha256((SEED / folder / name).read_bytes()).hexdigest(): answer for name, answer in answers.items()}


def answer_for(folder: str, data: bytes) -> dict:
    """La réponse préenregistrée pour ce fichier, s'il est l'un des exemples de seed/<folder>/."""
    answer = _answers(folder).get(hashlib.sha256(data).hexdigest())
    if answer is None:
        raise AIError(f"IA simulée : seuls les fichiers d'exemple de seed/{folder}/ sont reconnus.")
    return answer
