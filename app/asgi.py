from litestar import Litestar
from litestar.di import Provide

from app.api.controllers.documents import DocumentController
from app.api.controllers.health import HealthController
from app.config.settings import get_settings
from app.db.postgres import get_db_session


def create_app() -> Litestar:
    return Litestar(
        route_handlers=[HealthController, DocumentController],
        dependencies={
            "settings": Provide(get_settings, sync_to_thread=False),
            "db_session": Provide(get_db_session),
        },
    )