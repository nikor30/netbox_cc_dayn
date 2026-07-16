"""In-memory upload sessions with TTL cleanup. No database by design."""

import threading
import time
import uuid
from dataclasses import dataclass, field

from app.matcher import DeviceMatch
from app.models import CsvDocument, MappingResult


@dataclass
class UploadSession:
    id: str
    filename: str
    document: CsvDocument
    created_at: float
    matches: dict[str, DeviceMatch] = field(default_factory=dict)
    block_results: list[dict[str, MappingResult]] = field(default_factory=list)
    filled: bool = False
    fill_error: str = ""

    def apply_final_values(self) -> None:
        """Write final values into the document blocks for export."""
        for block, results in zip(self.document.blocks, self.block_results, strict=True):
            for variable, result in results.items():
                block.variables[variable] = result.final_value


class SessionStore:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, UploadSession] = {}
        self._lock = threading.Lock()

    def create(self, filename: str, document: CsvDocument) -> UploadSession:
        session = UploadSession(
            id=uuid.uuid4().hex,
            filename=filename,
            document=document,
            created_at=time.monotonic(),
        )
        with self._lock:
            self._purge_locked()
            self._sessions[session.id] = session
        return session

    def get(self, upload_id: str) -> UploadSession | None:
        with self._lock:
            self._purge_locked()
            return self._sessions.get(upload_id)

    def _purge_locked(self) -> None:
        deadline = time.monotonic() - self._ttl
        for key in [k for k, s in self._sessions.items() if s.created_at < deadline]:
            del self._sessions[key]
