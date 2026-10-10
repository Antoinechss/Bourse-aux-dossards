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
    demo_organizer_email: str


def load_settings() -> Settings:
    return Settings(
        app_env=os.getenv("APP_ENV", "local"),
        database_url=os.getenv("DATABASE_URL", "sqlite:///relais.db"),
        paypal_base_url=os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com"),
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", ""),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", ""),
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        demo_organizer_email=os.getenv("DEMO_ORGANIZER_EMAIL", ""),
    )


settings = load_settings()
