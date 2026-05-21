"""
Example Layover Application Entry Point.
Copy this into your specific Layover repo (e.g. `xyz-ops/src/main.py`).
"""
import os
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import JSONResponse

from ops_engine.adapters.github_adapter import GithubAdapter
from ops_engine.adapters.forgejo_adapter import ForgejoAdapter
from ops_engine.core.queue_manager import QueueManager
# Assume you have a local config_parser and webhook_handler
# from src.config_parser import engine_config
# from src.handlers.webhook_handler import process_webhook_event

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

queue_manager = QueueManager(rate_limit_delay_seconds=1.0)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await queue_manager.start()
    # Add your cron_loop dispatchers here
    yield
    await queue_manager.stop()

app = FastAPI(title="Example Ops Bot", lifespan=lifespan)

GITHUB_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "dummy")
GITHUB_TOKEN = os.getenv("GITHUB_APP_TOKEN", "dummy")
github_adapter = GithubAdapter(token=GITHUB_TOKEN, webhook_secret=GITHUB_SECRET)

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.post("/webhooks/github")
async def handle_github_webhook(request: Request):
    payload = await request.body()
    # Handle logic, enqueue events...
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"status": "queued"})
