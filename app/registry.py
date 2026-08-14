"""Document manifest backing ``GET /documents``.

Chunk metadata alone could answer "which documents are loaded?", but only by
scanning every vector. A tiny JSON manifest keeps document-level facts (page
count, chunk count, the chunk settings and embedding model actually used at
ingestion time) cheap to read and, more usefully, makes it obvious when the
store holds vectors produced under different settings.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from app.models import DocumentRecord

logger = logging.getLogger(__name__)

__all__ = ["DocumentRegistry"]


class DocumentRegistry:
    """A JSON-file map of ``doc_id -> DocumentRecord``."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._records: dict[str, DocumentRecord] = {}
        self._load()

    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            self._records = {
                item["doc_id"]: DocumentRecord.from_dict(item) for item in payload.get("documents", [])
            }
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("could not read document registry at %s (%s); starting empty", self._path, exc)
            self._records = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"documents": [record.to_dict() for record in self.list()]}
        # Write to a temp file in the same directory, then replace: a crash
        # mid-write cannot leave a truncated manifest behind.
        handle, temp_name = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(payload, file, indent=2)
            os.replace(temp_name, self._path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------ #
    def upsert(self, record: DocumentRecord) -> None:
        self._records[record.doc_id] = record
        self._save()

    def remove(self, doc_id: str) -> bool:
        if self._records.pop(doc_id, None) is None:
            return False
        self._save()
        return True

    def get(self, doc_id: str) -> DocumentRecord | None:
        return self._records.get(doc_id)

    def list(self) -> list[DocumentRecord]:
        return sorted(self._records.values(), key=lambda record: record.ingested_at, reverse=True)

    def __len__(self) -> int:
        return len(self._records)
