from litestar import Litestar
from litestar.di import Provide

from app.api.controllers.health import HealthController
from app.config.settings import get_settings


def create_app() -> Litestar:
    return Litestar(
        route_handlers=[HealthController],
        dependencies={"settings": Provide(get_settings, sync_to_thread=False)},
    )