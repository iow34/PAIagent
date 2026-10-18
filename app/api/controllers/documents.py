from typing import Annotated

from litestar import Controller, post
from litestar.datastructures import UploadFile
from litestar.enums import RequestEncodingType
from litestar.params import Body
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.documents import DocumentRepository
from app.services.document_service import DocumentService, IngestResult


class DocumentController(Controller):
    path = "/documents"

    @post()
    async def upload_document(
        self,
        data: Annotated[UploadFile, Body(media_type=RequestEncodingType.MULTI_PART)],
        db_session: AsyncSession,
    ) -> IngestResult:
        repository = DocumentRepository(db_session)
        service = DocumentService(repository)

        # тимчасово: user_id захардкоджений — реальна автентифікація прийде пізнішим кроком
        return await service.ingest(user_id="dev-user", upload=data)