"""The live document a request is bound to, for the length of one tool call.

The `X-Agora-Document` header names the map a tool call writes its layers into.
It also names the map whose sensor readings a tool may read, and a tool is
called by name with only its schema arguments, so the document travels in a
context variable the way the caller's bearer does.

Only a document id is carried. A share link token names a document too, but the
session token it resolves to belongs to no member, and agora answers its asset
routes to members.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_bound_document: ContextVar[str | None] = ContextVar("bound_document", default=None)


def document_id_of(binding: str | None) -> str | None:
    """`binding` as a document id, or None when it is a share link token."""
    if not binding:
        return None
    try:
        return str(uuid.UUID(binding))
    except ValueError:
        return None


@contextmanager
def bound_document_scope(document_id: str | None):
    """Run the block bound to `document_id`. None means bound to no document."""
    reset = _bound_document.set(document_id or None)
    try:
        yield
    finally:
        _bound_document.reset(reset)


def current_bound_document() -> str | None:
    return _bound_document.get()
