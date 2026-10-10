"""Faux client Claude pour les tests : il renvoie une réponse préparée et note ce qu'on lui a envoyé."""

from types import SimpleNamespace


class FakeClaude:
    def __init__(self, parsed=None, stop_reason="end_turn", error=None):
        self.parsed = parsed
        self.stop_reason = stop_reason
        self.error = error
        self.calls = []
        # Même chemin d'accès que le vrai client : client.beta.messages.parse(...)
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=self.parsed)

    @property
    def sent_file(self) -> dict:
        """Le bloc fichier de la dernière requête."""
        return self.calls[-1]["messages"][0]["content"][0]
