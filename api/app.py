from __future__ import annotations

from contextlib import asynccontextmanager
from threading import Event

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api import accounts, ai, image_tasks, register, system
from api.support import resolve_web_asset, start_image_cleanup_watcher, start_limited_account_watcher
from services.config import config


def create_app() -> FastAPI:
    app_version = config.app_version

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_event = Event()
        limited_account_thread = start_limited_account_watcher(stop_event)
        image_cleanup_thread = start_image_cleanup_watcher(stop_event)
        try:
            yield
        finally:
            stop_event.set()
            limited_account_thread.join(timeout=1)
            image_cleanup_thread.join(timeout=1)

    app = FastAPI(title="chatgpt2api", version=app_version, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(ai.create_router())
    app.include_router(accounts.create_router())
    app.include_router(image_tasks.create_router())
    app.include_router(register.create_router())
    app.include_router(system.create_router(app_version))
    if config.images_dir.exists():
        app.mount("/images", StaticFiles(directory=str(config.images_dir)), name="images")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_web(request: Request, full_path: str):
        host = str(request.headers.get("host") or "").split(":", 1)[0].lower()
        clean_path = full_path.strip("/")
        if host == "gpt.yunshuai.asia" and not clean_path.startswith("_next/"):
            asset = resolve_web_asset("gpt")
            if asset is not None:
                return FileResponse(asset)
        asset = resolve_web_asset(full_path)
        if asset is not None:
            return FileResponse(asset)
        if full_path.strip("/").startswith("_next/"):
            raise HTTPException(status_code=404, detail="Not Found")
        fallback = resolve_web_asset("")
        if fallback is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(fallback)

    return app
