from fastapi import FastAPI

from site_api.routes.system import router as system_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="crew.day site API",
        version="0.0.1",
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(system_router)
    return app


app = create_app()
