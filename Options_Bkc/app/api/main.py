from __future__ import annotations

from fastapi import FastAPI

from app.core.config import load_settings
from app.core.logging import configure_logging


def create_app() -> FastAPI:
    settings = load_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title=settings.app_name)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "env": settings.app_env}

    return app


app = create_app()
