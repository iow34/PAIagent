from typing import Annotated

from litestar import Controller, post
from litestar.datastructures import UploadFile
from litestar.di import NamedDependency, Provide
from litestar.enums import RequestEncodingType
from litestar.exceptions import HTTPException
from litestar.params import Body
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ingestion.validation import (
    MAX_FILE_BYTES,
    MAX_REQUEST_BYTES,
    FileToolLargeError,
    InvalidDocumentError,
    UnsupportedFileTypeError,
)
from app.security.authentication import get_access_context
from app.security.context import AccessContext
from app.services.document_service import DocumentService, IngestResult


class DocumentController(Controller):
    path = "/v1/documents"

    security = [{"ServiceAPIKey": []}]

    dependencies = {
        "access_context": Provide(get_access_context),
    }

    @post(request_max_body_size=MAX_REQUEST_BYTES)
    async def upload_document(
        self,
        data: Annotated[
            UploadFile,
            Body(media_type=RequestEncodingType.MULTI_PART),
        ],
        db_session_factory: NamedDependency[async_sessionmaker[AsyncSession]],
        access_context: NamedDependency[AccessContext],
    ) -> IngestResult:
        service = DocumentService(db_session_factory)

        try:
            raw_bytes = await data.read(MAX_FILE_BYTES + 1)

            return await service.ingest(
                context=access_context,
                filename=data.filename,
                raw_bytes=raw_bytes,
            )
        except UnsupportedFileTypeError as error:
            raise HTTPException(
                status_code=415,
                detail=str(error),
            ) from error
        except FileToolLargeError as error:
            raise HTTPException(
                status_code=413,
                detail=str(error),
            ) from error
        except InvalidDocumentError as error:
            raise HTTPException(
                status_code=400,
                detail=str(error),
            ) from error
