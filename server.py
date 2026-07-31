"""Folin API - app assembly."""
import logging

from fastapi import APIRouter, FastAPI
from starlette.middleware.cors import CORSMiddleware

import config  # noqa: F401 - loads .env first
import database
from routers import auth, family, members, reports, users

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Folin API")

api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {"message": "Folin API", "status": "ok"}


api_router.include_router(auth.router)
api_router.include_router(reports.router)
api_router.include_router(users.router)
api_router.include_router(family.router)
api_router.include_router(members.router)

app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    # HARDENING-2: use explicit allow-list from env when configured. Wildcard is kept as the
    # default for the preview / dev environment; production deploys should set
    # CORS_ALLOWED_ORIGINS to the exact origin(s).
    allow_credentials=config.CORS_ALLOWED_ORIGINS != ["*"],
    allow_origins=config.CORS_ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await database.connect()


@app.on_event("shutdown")
async def shutdown_db_client():
    await database.close()
