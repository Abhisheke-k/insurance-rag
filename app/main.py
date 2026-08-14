"""FastAPI application.

Endpoints
---------
``POST /ingest``       multipart upload of one or more endorsement PDFs
``POST /ask``          question -> grounded answer + citations
``GET  /documents``    what has been ingested, and under which settings
``GET  /health``       resolved configuration and store status
``GET  /ui``           the self-contained single-page frontend
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.generation import GenerationUnavailableError
from app.rag import RagService
from app.schemas import (
    AskRequest,
    AskResponse,
    CitationModel,
    DocumentModel,
    DocumentsResponse,
    HealthResponse,
    IngestedDocumentModel,
    IngestResponse,
    RetrievalHit,
)

logger = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
_PDF_MAGIC = b"%PDF"


def get_service(request: Request) -> RagService:
    service: RagService | None = getattr(request.app.state, "service", None)
    if service is None:  # pragma: no cover - only if startup failed
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the RAG service failed to start; check the server logs",
        )
    return service


ServiceDep = Annotated[RagService, Depends(get_service)]


def create_app(settings: Settings | None = None, service: RagService | None = None) -> FastAPI:
    """Build the application. Pass ``service`` to inject a pre-built pipeline."""
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "service", None) is None:
            logger.info("starting RAG service (chunk_size=%d, overlap=%d)", resolved.chunk_size, resolved.chunk_overlap)
            app.state.service = RagService(resolved)
        yield

    app = FastAPI(
        title="Endorsement RAG",
        version="0.1.0",
        summary="Ask questions about insurance endorsement packs, with page-level citations.",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.service = service

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------ #
    @app.get("/health", response_model=HealthResponse, tags=["ops"])
    def health(service: ServiceDep) -> HealthResponse:
        """Resolved configuration, store contents and generation readiness."""
        return HealthResponse(**service.health())

    # ------------------------------------------------------------------ #
    @app.post("/ingest", response_model=IngestResponse, tags=["ingest"])
    async def ingest(
        service: ServiceDep,
        files: Annotated[list[UploadFile], File(description="One or more endorsement PDFs")],
    ) -> IngestResponse:
        """Parse, chunk, embed and store PDFs.

        Ingestion is content-addressed: uploading the same bytes twice replaces
        the previous chunks instead of duplicating them, which is what makes
        re-running at a different CHUNK_SIZE safe.
        """
        if not files:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no files were uploaded")

        ingested: list[IngestedDocumentModel] = []
        errors: list[str] = []

        for upload in files:
            filename = upload.filename or "unnamed.pdf"
            try:
                data = await upload.read()
                _validate_pdf(filename, data, resolved.max_upload_bytes)
                result = service.ingest(data, filename)
            except ValueError as exc:
                logger.warning("ingest failed for %s: %s", filename, exc)
                errors.append(f"{filename}: {exc}")
                continue
            except Exception as exc:  # pragma: no cover - unexpected backend failure
                logger.exception("ingest crashed for %s", filename)
                errors.append(f"{filename}: {exc}")
                continue
            finally:
                await upload.close()
            ingested.append(IngestedDocumentModel.from_result(result))

        if not ingested and errors:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "; ".join(errors))

        return IngestResponse(
            documents=ingested,
            total_chunks=sum(document.chunks for document in ingested),
            errors=errors,
        )

    # ------------------------------------------------------------------ #
    @app.post("/ask", response_model=AskResponse, tags=["ask"])
    def ask(payload: AskRequest, service: ServiceDep) -> AskResponse:
        """Answer a question strictly from the ingested documents."""
        try:
            outcome = service.ask(payload.question, payload.top_k)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except GenerationUnavailableError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

        answer = outcome.answer
        cited_ids = {citation.chunk_id for citation in answer.citations}
        return AskResponse(
            answer=answer.answer,
            citations=[CitationModel.from_citation(citation) for citation in answer.citations],
            chunks_used=answer.chunks_used,
            found=answer.found,
            model=answer.model,
            retrieval=[
                RetrievalHit(
                    rank=rank,
                    chunk_id=chunk.chunk_id,
                    filename=chunk.filename,
                    page=chunk.page,
                    section=chunk.section,
                    score=round(chunk.score, 4),
                    cited=chunk.chunk_id in cited_ids,
                )
                for rank, chunk in enumerate(outcome.retrieved, start=1)
            ],
            notes=answer.notes,
        )

    # ------------------------------------------------------------------ #
    @app.get("/documents", response_model=DocumentsResponse, tags=["documents"])
    def documents(service: ServiceDep) -> DocumentsResponse:
        """List ingested documents, newest first."""
        records = service.documents()
        return DocumentsResponse(
            documents=[DocumentModel.from_record(record) for record in records],
            count=len(records),
            total_chunks=sum(record.chunks for record in records),
        )

    @app.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["documents"])
    def delete_document(doc_id: str, service: ServiceDep) -> None:
        """Remove a document and every chunk derived from it."""
        if not service.delete_document(doc_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown document {doc_id!r}")

    # ------------------------------------------------------------------ #
    if FRONTEND_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=FRONTEND_DIR, html=True), name="ui")

        @app.get("/", include_in_schema=False)
        def index() -> RedirectResponse:
            return RedirectResponse(url="/ui/")

    return app


def _validate_pdf(filename: str, data: bytes, max_bytes: int) -> None:
    """Reject anything that is not a plausibly-sized PDF before parsing it."""
    if not data:
        raise ValueError("file is empty")
    if len(data) > max_bytes:
        raise ValueError(f"file is {len(data)} bytes, over the {max_bytes} byte limit")
    if not data.lstrip()[:4] == _PDF_MAGIC:
        raise ValueError("not a PDF (missing %PDF header)")
    if not filename.lower().endswith(".pdf"):
        logger.info("accepting %s despite its extension: the content is a PDF", filename)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

#: Module-level ASGI app for `uvicorn app.main:app`.
app = create_app()
