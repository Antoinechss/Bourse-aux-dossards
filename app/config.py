"""Configuration de l'application, lue depuis les variables d'environnement (.env en local)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    app_env: str
    database_url: str
    paypal_base_url: str
    paypal_client_id: str
    paypal_client_secret: str
    paypal_webhook_id: str
    anthropic_api_key: str
    ai_mode: str
    demo_organizer_email: str
    reservation_minutes: int


def load_settings() -> Settings:
    return Settings(
        app_env=os.getenv("APP_ENV", "local"),
        database_url=os.getenv("DATABASE_URL", "sqlite:///relais.db"),
        paypal_base_url=os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com"),
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", ""),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", ""),
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        # "real" appelle Claude ; "fake" renvoie des réponses préenregistrées pour les fichiers de seed/.
        ai_mode=os.getenv("AI_MODE", "real"),
        demo_organizer_email=os.getenv("DEMO_ORGANIZER_EMAIL", ""),
        # Temps laissé à l'acheteur pour payer le dossard qu'il a réservé.
        reservation_minutes=int(os.getenv("RESERVATION_MINUTES", "15")),
    )


settings = load_settings()
