"""Point d'entrée FastAPI : crée l'application, sert la page d'accueil et lance le ménage périodique."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from app.db import SessionDep, create_db_and_tables, engine
from app.models import Race
from app.registry_mock.router import router as registry_router
from app.routers.inbox import router as inbox_router
from app.routers.organizer import router as organizer_router
from app.routers.runner import router as runner_router
from app.routers.webhooks import router as webhooks_router
from app.templating import templates
from app.transfers import flow

BASE_DIR = Path(__file__).parent
HOUSEKEEPING_SECONDS = 5
logger = logging.getLogger(__name__)


def run_housekeeping() -> None:
    with Session(engine) as session:
        flow.housekeeping(session)


async def housekeeping_loop() -> None:
    """Toutes les quelques secondes : offres expirées, transferts à faire avancer, versements à confirmer."""
    while True:
        await asyncio.sleep(HOUSEKEEPING_SECONDS)
        try:
            # Dans un fil à part : le ménage parle à la base et à PayPal, il ne doit pas figer le serveur.
            await asyncio.to_thread(run_housekeeping)
        except Exception:
            logger.exception("Ménage périodique en échec")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Au démarrage du serveur : crée les tables qui n'existent pas encore, puis lance le ménage.
    create_db_and_tables()
    housekeeping = asyncio.create_task(housekeeping_loop())
    yield
    housekeeping.cancel()


app = FastAPI(title="Relais", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(registry_router)
app.include_router(organizer_router)
app.include_router(runner_router)
app.include_router(inbox_router)
app.include_router(webhooks_router)


@app.get("/", response_class=HTMLResponse)
def home(request: Request, session: SessionDep):
    races = session.exec(select(Race).order_by(Race.event_date)).all()
    return templates.TemplateResponse(request, "index.html", {"races": races})
