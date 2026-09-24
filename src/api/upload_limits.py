from __future__ import annotations

import os
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from fastapi import HTTPException, Request, status
from starlette.datastructures import UploadFile
from starlette.types import Message, Receive

from src.api.daily_budget import DailyBudget, utc_today, whole_number_limit

UPLOAD_MAX_REQUEST_MEGABYTES_ENV = "GEOLANG_UPLOAD_MAX_REQUEST_MEGABYTES"
UPLOAD_MAX_FILE_MEGABYTES_ENV = "GEOLANG_UPLOAD_MAX_FILE_MEGABYTES"
UPLOAD_MAX_ZIP_ENTRIES_ENV = "GEOLANG_UPLOAD_MAX_ZIP_ENTRIES"
UPLOAD_MAX_UNZIPPED_MEGABYTES_ENV = "GEOLANG_UPLOAD_MAX_UNZIPPED_MEGABYTES"
UPLOAD_FILES_PER_DAY_ENV = "GEOLANG_UPLOAD_FILES_PER_DAY"
UPLOAD_FILES_PER_CALLER_PER_DAY_ENV = "GEOLANG_UPLOAD_FILES_PER_CALLER_PER_DAY"
UPLOAD_MEGABYTES_PER_DAY_ENV = "GEOLANG_UPLOAD_MEGABYTES_PER_DAY"
UPLOAD_MEGABYTES_PER_CALLER_PER_DAY_ENV = "GEOLANG_UPLOAD_MEGABYTES_PER_CALLER_PER_DAY"

BYTES_PER_MEGABYTE = 1024 * 1024
UPLOAD_COPY_CHUNK_BYTES = BYTES_PER_MEGABYTE
# the one file plus thread_id
UPLOAD_FORM_FILE_LIMIT = 1
UPLOAD_FORM_FIELD_LIMIT = 1

FILES_SPENT_TODAY_REPLY = "The upload budget for today is used up. Try again tomorrow."
CALLER_FILES_SPENT_REPLY = "You have used today's upload budget. Try again tomorrow."
BYTES_SPENT_TODAY_REPLY = "The upload size budget for today is used up. Try again tomorrow."
CALLER_BYTES_SPENT_REPLY = (
    "This file would go over today's upload size budget. Try again tomorrow."
)


def megabyte_limit(name: str, unit: str = "megabytes") -> int | None:
    megabytes = whole_number_limit(name, unit)
    return None if megabytes is None else megabytes * BYTES_PER_MEGABYTE


def too_large(what: str, limit: int) -> HTTPException:
    return HTTPException(
        status.HTTP_413_CONTENT_TOO_LARGE, f"{what} is over the limit of {limit} bytes"
    )


@dataclass(frozen=True)
class UploadLimits:
    max_request_bytes: int | None
    max_file_bytes: int | None
    max_zip_entries: int | None
    max_unzipped_bytes: int | None

    @classmethod
    def from_environment(cls) -> UploadLimits:
        return cls(
            megabyte_limit(UPLOAD_MAX_REQUEST_MEGABYTES_ENV),
            megabyte_limit(UPLOAD_MAX_FILE_MEGABYTES_ENV),
            whole_number_limit(UPLOAD_MAX_ZIP_ENTRIES_ENV, "zip entries"),
            megabyte_limit(UPLOAD_MAX_UNZIPPED_MEGABYTES_ENV),
        )


def receive_at_most(receive: Receive, byte_limit: int) -> Receive:
    received = 0

    async def limited_receive() -> Message:
        nonlocal received
        message = await receive()
        received += len(message.get("body", b""))
        if received > byte_limit:
            raise too_large("the request body", byte_limit)
        return message

    return limited_receive


def bounded_request(request: Request, byte_limit: int) -> Request:
    declared = request.headers.get("content-length", "")
    if declared.isdecimal() and int(declared) > byte_limit:
        raise too_large("the request body", byte_limit)
    return Request(request.scope, receive_at_most(request.receive, byte_limit))


def upload_form(request: Request, limits: UploadLimits):
    bounded = request
    if limits.max_request_bytes is not None:
        bounded = bounded_request(request, limits.max_request_bytes)
    return bounded.form(
        max_files=UPLOAD_FORM_FILE_LIMIT, max_fields=UPLOAD_FORM_FIELD_LIMIT
    )


def checked_upload_bytes(upload: UploadFile, limits: UploadLimits) -> int:
    size = upload.file.seek(0, os.SEEK_END)
    upload.file.seek(0)
    if limits.max_file_bytes is not None and size > limits.max_file_bytes:
        raise too_large("the file", limits.max_file_bytes)
    return size


async def save_upload(upload: UploadFile, destination: Path) -> None:
    with open(destination, "wb") as saved:
        while chunk := await upload.read(UPLOAD_COPY_CHUNK_BYTES):
            saved.write(chunk)


# extraction stops each entry at its declared size
def checked_unzipped_bytes(upload: UploadFile, target: Path, limits: UploadLimits) -> int:
    # the target itself may already exist as a symlink
    confined = target.parent.resolve() / target.name
    try:
        with zipfile.ZipFile(upload.file) as archive:
            entries = archive.infolist()
    except zipfile.BadZipFile:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the file is not a readable zip")
    finally:
        upload.file.seek(0)

    if limits.max_zip_entries is not None and len(entries) > limits.max_zip_entries:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"the zip holds {len(entries)} entries, the limit is {limits.max_zip_entries}",
        )
    total = sum(entry.file_size for entry in entries)
    if limits.max_unzipped_bytes is not None and total > limits.max_unzipped_bytes:
        raise too_large("the unzipped content", limits.max_unzipped_bytes)
    for entry in entries:
        if not (confined / entry.filename).resolve().is_relative_to(confined):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"zip entry '{entry.filename}' points outside the folder it unzips into",
            )
    return total


class UploadBudget:
    def __init__(
        self,
        files_per_day: int | None,
        files_per_caller_per_day: int | None,
        bytes_per_day: int | None,
        bytes_per_caller_per_day: int | None,
        today: Callable[[], date] = utc_today,
    ):
        self.file_budget = DailyBudget(
            "upload file",
            files_per_day,
            files_per_caller_per_day,
            FILES_SPENT_TODAY_REPLY,
            CALLER_FILES_SPENT_REPLY,
            today,
        )
        self.byte_budget = DailyBudget(
            "upload byte",
            bytes_per_day,
            bytes_per_caller_per_day,
            BYTES_SPENT_TODAY_REPLY,
            CALLER_BYTES_SPENT_REPLY,
            today,
        )

    @classmethod
    def from_environment(cls) -> UploadBudget:
        return cls(
            whole_number_limit(UPLOAD_FILES_PER_DAY_ENV, "uploads per day"),
            whole_number_limit(UPLOAD_FILES_PER_CALLER_PER_DAY_ENV, "uploads per day"),
            megabyte_limit(UPLOAD_MEGABYTES_PER_DAY_ENV, "megabytes per day"),
            megabyte_limit(UPLOAD_MEGABYTES_PER_CALLER_PER_DAY_ENV, "megabytes per day"),
        )

    # None when the upload is counted, otherwise the reply refusing it
    def spend(self, caller: str | None, stored_bytes: int) -> str | None:
        refused = self.file_budget.refusal(caller) or self.byte_budget.refusal(
            caller, stored_bytes
        )
        if refused is not None:
            return refused
        self.file_budget.record(caller)
        self.byte_budget.record(caller, stored_bytes)
        return None
