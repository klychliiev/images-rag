from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import main as routes_module


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    app = FastAPI(
        title="ODT Document Processing Service",
        description="Process ODT files and index them in Pinecone",
        version="1.0.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(routes_module.app)

    @app.get("/")
    async def root():
        return {
            "service": "ODT Document Processing Service",
            "status": "running",
            "docs": "/docs",
        }

    return app


app = create_app()
