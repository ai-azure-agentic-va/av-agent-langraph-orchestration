"""The files an analyst attaches to an STTM message, as text the STTM tools can read.

An attached PDF becomes its text in reading order, with each ruled table as
tab-separated rows (the form of a table copied from a spreadsheet), so a
specification or a set of instructions in a PDF is read like a pasted one. A
table that runs on over the next page is joined into one, its repeated header
dropped. Nothing is guessed: a table without ruling lines comes out as plain
text, and a PDF without a text layer (a scan), one that cannot be opened, or an
image gives a note instead of text.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pdfplumber

from v1.core.tools.sttm.spec import starts_another_table

logger = logging.getLogger(__name__)

PDF = "application/pdf"
# What one message's attachments may add to it: the PDF size, its pages, its
# characters, all of them together, and the time spent reading each one.
MAX_BYTES = 5 * 1024 * 1024
MAX_PAGES = 100
MAX_CHARS = 100_000
MAX_MESSAGE_CHARS = 200_000
MAX_SECONDS = 20.0
# Text between two tables on consecutive pages up to this long is a page footer
# and header, so the second table may carry on the first.
_PAGE_BREAK_CHARS = 300
# The line that opens a file's text (attachments_text) or a skill's block (the
# frontend's) in a message; a line "</document>" or "</user_skill>" closes it.
_PART_START = re.compile(r'<(document|user_skill) name="([^"]*)"(?: pages="\d+")?>')


@dataclass(frozen=True)
class Read:
    """What one attached file gives: its text (empty when nothing was read) and notes."""

    name: str
    text: str = ""
    pages: int = 0
    notes: tuple[str, ...] = ()


@dataclass
class _Table:
    rows: list[list[str]]
    page: int
    # Page footers and headers the table ran on past, shown after it.
    after: list[str] = field(default_factory=list)


# Files read lately, by name and content: every task in a turn reads the same
# message.
_CACHE_SIZE = 16
_CACHE: OrderedDict[tuple[str, str], Read] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def attachment_name(block: Mapping[str, Any]) -> str:
    """The file name an attached content block gives, or what kind of file it is."""

    file = block.get("file") if isinstance(block.get("file"), Mapping) else {}
    for holder in (block.get("extras"), block.get("metadata"), block, file):
        if isinstance(holder, Mapping):
            name = holder.get("filename") or holder.get("name")
            if isinstance(name, str) and name.strip():
                return " ".join(name.split())
    image = str(block.get("type", "")).startswith("image")
    return "an attached image" if image else "an attached file"


def _payload(block: Mapping[str, Any]) -> tuple[str, str]:
    """(media type, base64 content) of an attached content block; empty when not inline."""

    mime = str(block.get("mime_type") or block.get("mimeType") or "")
    data = block.get("base64") or (
        block.get("data") if block.get("source_type") == "base64" else ""
    )
    file = block.get("file")
    if not data and isinstance(file, Mapping):
        data = str(file.get("file_data") or "")
    if isinstance(data, str) and data.startswith("data:") and "," in data:
        head, data = data.split(",", 1)
        mime = mime or head[5:].split(";", 1)[0]
    return mime.casefold(), data if isinstance(data, str) else ""


def read_attachment(block: Mapping[str, Any]) -> Read:
    """The text of one attached file, or why it was not read."""

    name = attachment_name(block)
    mime, data = _payload(block)
    if mime.startswith("image/") or str(block.get("type", "")).startswith("image"):
        return Read(
            name,
            notes=(f"{name} was not read: images cannot be read. Ask for its content as text.",),
        )
    if mime != PDF:
        return Read(
            name,
            notes=(f"{name} was not read: only PDFs are. Ask for its content as text.",),
        )
    if not data:
        return Read(name, notes=(f"{name} came without its content, so it was not read.",))
    if len(data) * 3 // 4 > MAX_BYTES:
        return Read(
            name,
            notes=(
                (
                    f"{name} is larger than {MAX_BYTES // (1024 * 1024)} MB, so it was not read: "
                    "ask for the part that matters as text, or as a smaller PDF."
                ),
            ),
        )
    key = (name, hashlib.sha256(data.encode("ascii", "replace")).hexdigest())
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return cached
    try:
        content = base64.b64decode(data, validate=False)
    except (binascii.Error, ValueError):
        return Read(name, notes=(f"{name} could not be decoded, so it was not read.",))
    read = read_pdf(content, name)
    with _CACHE_LOCK:
        _CACHE[key] = read
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return read


def _cell(value: Any) -> str:
    return " ".join(str(value or "").split())


def _between(page: Any, top: float, bottom: float) -> str:
    """The page's text from ``top`` down to ``bottom``, the full width of the page."""

    x0, page_top, x1, page_bottom = page.bbox
    top, bottom = max(top, page_top), min(bottom, page_bottom)
    if bottom - top < 1:
        return ""
    return (page.crop((x0, top, x1, bottom)).extract_text() or "").strip()


def _carried_on(parts: list[str | _Table], rows: list[list[str]], page: int) -> int | None:
    """Where in ``parts`` the table is that a page's first table carries on, if it does.

    That is the last table of the page before, with only a page footer and
    header between them and as many columns, unless the new table starts with
    a header of its own (:func:`starts_another_table`); a repeated header is
    not one.
    """

    last = next((i for i in range(len(parts) - 1, -1, -1) if isinstance(parts[i], _Table)), None)
    if last is None:
        return None
    previous = parts[last]
    gap = sum(len(part) for part in parts[last + 1 :] if isinstance(part, str))
    if (
        not isinstance(previous, _Table)
        or previous.page != page - 1
        or len(rows[0]) != len(previous.rows[0])
        or gap > _PAGE_BREAK_CHARS
        or starts_another_table(previous.rows[0], rows[0])
    ):
        return None
    return last


def _read_page(page: Any, number: int, parts: list[str | _Table]) -> None:
    """Add a page's text and ruled tables to ``parts``, top down."""

    top = page.bbox[1]
    tables = sorted(page.find_tables(), key=lambda table: (table.bbox[1], table.bbox[0]))
    for index, table in enumerate(tables):
        text = _between(page, top, table.bbox[1])
        if text:
            parts.append(text)
        top = max(top, table.bbox[3])
        rows = [[_cell(value) for value in row] for row in table.extract()]
        rows = [row for row in rows if any(row)]
        if not rows:
            continue
        at = _carried_on(parts, rows, number) if index == 0 else None
        if at is None:
            parts.append(_Table(rows, number))
            continue
        previous = parts[at]
        if isinstance(previous, _Table):
            # The footer and header between the two halves go after the table.
            previous.after += [part for part in parts[at + 1 :] if isinstance(part, str)]
            del parts[at + 1 :]
            previous.rows += rows[1:] if rows[0] == previous.rows[0] else rows
            previous.page = number
    text = _between(page, top, page.bbox[3])
    if text:
        parts.append(text)


def read_pdf(content: bytes, name: str) -> Read:
    """``content``'s text in reading order, its ruled tables as tab-separated rows."""

    notes: list[str] = []
    parts: list[str | _Table] = []
    started = time.monotonic()
    try:
        with pdfplumber.open(io.BytesIO(content)) as document:
            total = len(document.pages)
            read = 0
            for page in document.pages[:MAX_PAGES]:
                # The first page is always read.
                if read and time.monotonic() - started > MAX_SECONDS:
                    break
                read += 1
                _read_page(page, read, parts)
    except Exception as error:  # noqa: BLE001 - a damaged or password-protected file
        logger.warning("Could not read an attached PDF: %s", type(error).__name__)
        return Read(
            name,
            notes=(
                (
                    f"{name} could not be opened (damaged, or protected by a password), so it "
                    "was not read: ask for its content as text."
                ),
            ),
        )
    if read < total:
        first = "page 1 was" if read == 1 else f"pages 1-{read} were"
        notes.append(
            f"{name}: of its {total} pages only {first} read; ask for the rest as text or in "
            "another message."
        )
    lines: list[str] = []
    for part in parts:
        if isinstance(part, _Table):
            # A blank line ends the table, so a table after it is read as one of its own.
            lines += ["\t".join(row) for row in part.rows] + [""]
            lines += part.after
        else:
            lines.append(part)
    # Blank lines at either end go; a last row's empty cells stay.
    text = "\n".join(lines).strip("\n")
    if not text:
        return Read(
            name,
            pages=total,
            notes=(
                *notes,
                (
                    f"{name} has no text to read (a scanned document?): ask for its content as "
                    "text, or a PDF with selectable text."
                ),
            ),
        )
    if len(text) > MAX_CHARS:
        cut = text.rfind("\n", 0, MAX_CHARS)
        text = text[: cut if cut > 0 else MAX_CHARS]
        notes.append(
            f"{name}: only its first {len(text):,} characters were read; ask for the rest as "
            "text or in another message."
        )
    return Read(name, text, total, tuple(notes))


def attachments_text(blocks: Sequence[Mapping[str, Any]]) -> tuple[list[str], list[str]]:
    """Each attached file of a message as a ``<document>`` block, and the notes on them.

    Together they add at most :data:`MAX_MESSAGE_CHARS` to the message; a file
    past that is not read.
    """

    documents: list[str] = []
    notes: list[str] = []
    used = 0
    for block in blocks:
        read = read_attachment(block)
        notes += read.notes
        if not read.text:
            continue
        if used + len(read.text) > MAX_MESSAGE_CHARS:
            notes.append(
                f"{read.name} was not read: this message's attachments hold more text than "
                "can be read at once. Ask for it in another message."
            )
            continue
        used += len(read.text)
        # The name goes in a tag: it must not end the tag or open another one.
        name = read.name.replace('"', "'").replace("<", "(").replace(">", ")")
        documents.append(f'<document name="{name}" pages="{read.pages}">\n{read.text}\n</document>')
    return documents, notes


def message_parts(content: str | Sequence[Any]) -> tuple[list[str], list[Mapping[str, Any]]]:
    """A message's text blocks (the typed text, a skill's block) and its attached files."""

    if isinstance(content, str):
        return ([content] if content.strip() else []), []
    texts: list[str] = []
    files: list[Mapping[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, Mapping):
            if block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
            else:
                files.append(block)
    return [text for text in texts if text.strip()], files


def read_message(content: str | Sequence[Any]) -> tuple[str, list[str]]:
    """A message's text with each attached PDF's after it, and notes on the files not read."""

    texts, files = message_parts(content)
    documents, notes = attachments_text(files)
    return "\n\n".join([*texts, *documents]), notes


@dataclass(frozen=True)
class Part:
    """A part of a message's text: the typed text, a skill's block or a file's text.

    ``first`` and ``last`` are its lines, counted from 0 as ``splitlines`` gives
    them; the tags around a skill's block or a file's text are not part of it.
    """

    kind: str  # "text", "user_skill" or "document"
    name: str
    first: int
    last: int


def message_layout(text: str) -> list[Part]:
    """The parts of a message's text (:func:`read_message`), in order."""

    parts: list[Part] = []
    lines = text.splitlines()
    first, kind, name = 0, "text", ""
    for number, line in enumerate(lines):
        if kind == "text":
            start = _PART_START.fullmatch(line)
            if start is None:
                continue
            if number > first:
                parts.append(Part("text", "", first, number - 1))
            first, kind, name = number + 1, start.group(1), start.group(2)
        elif line == f"</{kind}>":
            parts.append(Part(kind, name, first, number - 1))
            first, kind, name = number + 1, "text", ""
    if first < len(lines):
        parts.append(Part(kind, name, first, len(lines) - 1))
    return parts


def document_at(text: str, line: int) -> str | None:
    """The name of the attached file whose text holds line ``line`` (from 1), if one does."""

    return next(
        (
            part.name
            for part in message_layout(text)
            if part.kind == "document" and part.first < line <= part.last + 1
        ),
        None,
    )


def without_documents(text: str) -> str:
    """``text`` without the attached files' text, and without the tags of its parts."""

    lines = text.splitlines()
    return "\n".join(
        line
        for part in message_layout(text)
        if part.kind != "document"
        for line in lines[part.first : part.last + 1]
    )


__all__ = [
    "MAX_MESSAGE_CHARS",
    "PDF",
    "Part",
    "Read",
    "attachment_name",
    "attachments_text",
    "document_at",
    "message_layout",
    "message_parts",
    "read_attachment",
    "read_message",
    "read_pdf",
    "without_documents",
]
