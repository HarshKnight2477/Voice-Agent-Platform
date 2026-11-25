"""Keyset (cursor) pagination. Stable under inserts, and never scans past the page it returns.

Lists are ordered newest first by (created_at, id). The cursor is an opaque, URL-safe
encoding of the last row's sort key; clients pass it back unchanged.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import and_, or_
from sqlalchemy.orm import InstrumentedAttribute

from voice_platform.api.errors import AppError

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

Limit = Annotated[int, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query(max_length=200)]


class Page[T](BaseModel):
    data: list[T]
    next_cursor: str | None


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    raw = json.dumps([created_at.isoformat(), str(row_id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created, row_id = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(created), uuid.UUID(row_id)
    except (ValueError, TypeError) as exc:
        raise AppError(400, "invalid_cursor", "The page cursor is invalid.") from exc


def apply_keyset(
    stmt: Any,
    created_col: InstrumentedAttribute[datetime],
    id_col: InstrumentedAttribute[uuid.UUID],
    cursor: str | None,
    limit: int,
) -> Any:
    """Newest first. Fetches limit + 1 rows so the caller can tell if another page exists."""
    if cursor:
        created, row_id = decode_cursor(cursor)
        stmt = stmt.where(or_(created_col < created, and_(created_col == created, id_col < row_id)))
    return stmt.order_by(created_col.desc(), id_col.desc()).limit(limit + 1)
