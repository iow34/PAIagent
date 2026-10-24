import msgspec
from litestar import Controller, get
from litestar.di import NamedDependency

from app.config.settings import Settings


class HealthStatus(msgspec.Struct):
    status: str
    database_configured: bool
    openai_configured: bool


class HealthController(Controller):
    path = "/health"

    @get()
    async def health_check(
        self,
        settings: NamedDependency[Settings],
    ) -> HealthStatus:
        return HealthStatus(
            status="ok",
            database_configured=bool(settings.database_url),
            openai_configured=bool(settings.openai_api_key),
        )
