"""FastAPI app assembly. Run with: uvicorn app.main:app"""
import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.config import settings
from agents.base import client as model_client
from db import session as database
from routers import admin, auth, early_access, family, members, payments, reports, users
from services.jobs import jobs
from services.push_notifications import push_notifications

API_PREFIX = "/api"
API_TITLE = "Folin API"
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await database.connect()
    await jobs.start()
    await push_notifications.start()
    try:
        yield
    finally:
        await push_notifications.stop()
        await jobs.stop()
        await model_client.close()
        await database.close()


app = FastAPI(title=API_TITLE, lifespan=lifespan)

api_router = APIRouter(prefix=API_PREFIX)


@api_router.get("/")
async def root():
    return {"message": API_TITLE, "status": "ok"}


for r in (auth.router, reports.router, users.router, family.router, members.router,
          payments.router, early_access.router, admin.router):
    api_router.include_router(r)

app.include_router(api_router)

# A wildcard origin cannot carry credentials (browsers refuse it); production sets the exact origin.
app.add_middleware(
    CORSMiddleware,
    allow_credentials=settings.cors_allowed_origins != ["*"],
    allow_origins=settings.cors_allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
