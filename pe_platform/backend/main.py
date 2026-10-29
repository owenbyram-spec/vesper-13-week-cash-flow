"""Compose the native cash-flow/AI platform with explicit local adapters."""
from __future__ import annotations

from pathlib import Path

from pe_platform.provenance import verify_source


def create_app(*, data_dir: Path, port: int = 8771, frontend_origin: str | None = None, testing: bool = False):
    source = verify_source()
    from . import bootstrap
    runtime = bootstrap.configure(data_dir, port=port,
                                  frontend_origin=frontend_origin or "http://127.0.0.1:8770",
                                  testing=testing)
    from fastapi import FastAPI
    application = FastAPI(title="Vesper & Co — PE Cash Flow", docs_url=None,
                          redoc_url=None, openapi_url=None)
    bootstrap.register(application)
    from . import ai
    ai.register(application, runtime)

    @application.get("/local/source")
    def source_identity():
        return source

    return application
