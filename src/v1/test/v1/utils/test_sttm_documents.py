"""Tests for the files attached to an STTM message (``v1.core.tools.sttm.documents``).

An attached PDF becomes text the STTM tools read like a pasted message:

- its text in reading order, each ruled table as tab-separated rows, which the
  specification reader reads like a table copied from a spreadsheet;
- a table that runs on over the next page is one table, with or without its
  header repeated there, and the page footer and header between come after
  it; a table with a header of its own on the next page is another table;
- a table without ruling lines comes out as plain lines, which the
  specification reader reports as a table that lost its structure;
- a file that is not read says why: an image, another kind of file, one sent
  without its content, too large, undecodable, damaged, or a scan with no
  text; and the parts past the page, character, time and per-message limits
  are named;
- the shapes the frontend and the message formats send a file in, the cache
  that reads a file once for every task of a turn, how a message's text,
  documents and notes are put together, and its parts: the typed text, a
  skill's block and each file's text, and the message without its files.

The PDFs come from a small writer here: lines of text and tables of
130-point cells, ruled or not, top down.

Runs standalone (``python test_sttm_documents.py``) or under pytest.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from v1.core.tools.sttm import documents
from v1.core.tools.sttm.documents import (
    PDF,
    Part,
    Read,
    attachments_text,
    document_at,
    message_layout,
    message_parts,
    read_attachment,
    read_message,
    read_pdf,
    without_documents,
)
from v1.core.tools.sttm.spec import parse_specification

# -- writing PDFs

_Item = str | list[list[str]]


def _escaped(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _page(items: Sequence[_Item], ruled: bool) -> bytes:
    """A page's content: each line of text or table below the one before it."""

    ops: list[str] = []
    y = 760
    for item in items:
        if isinstance(item, str):
            ops.append(f"BT /F1 10 Tf 50 {y} Td ({_escaped(item)}) Tj ET")
            y -= 16
            continue
        for row in item:
            y -= 20
            for column, cell in enumerate(row):
                x = 50 + 130 * column
                if ruled:
                    ops.append(f"{x} {y} 130 20 re S")
                ops.append(f"BT /F1 9 Tf {x + 3} {y + 6} Td ({_escaped(cell)}) Tj ET")
        y -= 10
    return "\n".join(ops).encode("latin-1")


def _pdf(*pages: Sequence[_Item], ruled: bool = True) -> bytes:
    """A PDF of ``pages``, each a list of lines of text and tables (lists of rows)."""

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # The page tree, once the pages have their numbers.
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids: list[str] = []
    for items in pages:
        content = _page(items, ruled)
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 900 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objects)} 0 R >>"
            ).encode()
        )
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def _block(content: bytes, name: str = "spec.pdf", mime: str = PDF) -> dict[str, Any]:
    """An attached file as the frontend sends it."""

    return {
        "type": "file",
        "base64": base64.b64encode(content).decode(),
        "mime_type": mime,
        "extras": {"filename": name},
    }


@contextmanager
def _patched(**values: Any) -> Iterator[None]:
    """Run with the documents module's ``values`` and an empty cache."""

    saved = {name: getattr(documents, name) for name in values}
    for name, value in values.items():
        setattr(documents, name, value)
    documents._CACHE.clear()
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(documents, name, value)
        documents._CACHE.clear()


def _read(block: dict[str, Any]) -> Read:
    """``block`` read afresh."""

    with _patched():
        return read_attachment(block)


def _tsv(rows: list[list[str]]) -> str:
    return "\n".join("\t".join(row) for row in rows)


# A made-up specification: CoreBank's daily accounts feed.
_INSTRUCTIONS = "Source system: CoreBank. Source: daily accounts feed."
_HEADER = ["Field Name", "Data Type", "Length", "Description"]
_ROWS = [
    ["CUSTOMER_ID", "VARCHAR", "20", "Customer identifier"],
    ["ACCOUNT_NO", "VARCHAR", "16", "Account number"],
    ["OPEN_DATE", "DATE", "", "Date opened"],
]
_MORE_ROWS = [
    ["BRANCH_ID", "VARCHAR", "8", "Branch identifier"],
    ["BALANCE", "DECIMAL", "18,2", "Ledger balance"],
]


# -- text and tables


def test_a_ruled_table_is_read_as_tab_separated_rows_in_reading_order() -> None:
    # Its empty rows, and an empty ruled box (a form's), are left out.
    table = [_HEADER, _ROWS[0], ["", "", "", ""], *_ROWS[1:]]
    content = _pdf([_INSTRUCTIONS, [["", ""]], "Specification:", table, "Questions follow."])
    read = read_pdf(content, "spec.pdf")
    text = f"{_INSTRUCTIONS}\nSpecification:\n{_tsv([_HEADER, *_ROWS])}\n\nQuestions follow."
    assert read == Read("spec.pdf", text, pages=1)

    # The specification reader reads it like a table pasted from a spreadsheet.
    parsed = parse_specification(read.text)
    assert parsed.tables == [
        (
            "Table at line 3: 3 attribute(s); columns name = 'Field Name', data_type = "
            "'Data Type', length = 'Length', description = 'Description'"
        )
    ]
    assert [(a.name, a.line, a.data_type, a.length) for a in parsed.attributes] == [
        ("CUSTOMER_ID", 4, "VARCHAR", "20"),
        ("ACCOUNT_NO", 5, "VARCHAR", "16"),
        ("OPEN_DATE", 6, "DATE", None),
    ]


def test_a_table_that_runs_on_over_the_next_page_is_one_table() -> None:
    table = _tsv([_HEADER, *_ROWS, *_MORE_ROWS])
    # The header repeated on the next page, or not.
    for second in ([_HEADER, *_MORE_ROWS], _MORE_ROWS):
        content = _pdf(
            [_INSTRUCTIONS, [_HEADER, *_ROWS], "Page 1 of 2"],
            ["CoreBank accounts specification", second, "End of the specification."],
        )
        # The page footer and header go after the table.
        assert read_pdf(content, "spec.pdf").text == (
            f"{_INSTRUCTIONS}\n{table}\n\nPage 1 of 2\nCoreBank accounts specification\n"
            "End of the specification."
        )
        assert parse_specification(read_pdf(content, "spec.pdf").text).tables == [
            (
                "Table at line 2: 5 attribute(s); columns name = 'Field Name', data_type = "
                "'Data Type', length = 'Length', description = 'Description'"
            )
        ]

    # Over four pages: each page break is measured on its own.
    title = "CoreBank accounts specification"
    pages = [
        [[_HEADER, *_ROWS]],
        [title, _MORE_ROWS],
        [title, [["LAST_TXN_DATE", "DATE", "", "Last transaction date"]]],
        [title, [["STATUS_CODE", "CHAR", "1", "Account status"]]],
    ]
    footers = [
        f"CoreBank daily accounts feed, specification version 3, draft for review: page {n} of 4"
        for n in range(1, 5)
    ]
    content = _pdf(*[[*page, footer] for page, footer in zip(pages, footers, strict=True)])
    rows = [_HEADER, *_ROWS, *_MORE_ROWS, *pages[2][1], *pages[3][1]]
    between = [line for footer in footers[:3] for line in (footer, title)]
    assert read_pdf(content, "spec.pdf").text == "\n".join([_tsv(rows), "", *between, footers[3]])


def test_a_table_with_a_header_of_its_own_on_the_next_page_is_another_table() -> None:
    columns = "the names it has no type, length, required or description column"
    others = {
        # Another number of columns.
        "three columns": (
            [["Field Name", "Classification", "Handling"], ["CUSTOMER_ID", "Restricted", "Mask"]],
            (
                f"Skipped the table at line 7: besides {columns} (Field Name, Classification, "
                "Handling)."
            ),
        ),
        # A header a specification's table has.
        "specification": (
            [["Column Name", "Data Type", "Length", "Description"], *_MORE_ROWS],
            None,
        ),
        # A derived-field table's header.
        "derived fields": (
            [
                ["Derived Field", "Logic", "Join Key", "Owner"],
                ["FULL_NAME", "FIRST_NAME + LAST_NAME", "CUSTOMER_ID", "Data team"],
            ],
            None,
        ),
        # The names column headed as the table before headed it.
        "classification": (
            [
                ["Field Name", "Classification", "Handling", "Owner"],
                ["CUSTOMER_ID", "Restricted", "Mask", "Data team"],
            ],
            (
                f"Skipped the table at line 7: besides {columns} (Field Name, Classification, "
                "Handling, Owner)."
            ),
        ),
    }
    for label, (second, note) in others.items():
        content = _pdf([[_HEADER, *_ROWS], "Page 1 of 2"], [second])
        text = read_pdf(content, "spec.pdf").text
        assert text == f"{_tsv([_HEADER, *_ROWS])}\n\nPage 1 of 2\n{_tsv(second)}", label
        parsed = parse_specification(text)
        names = [attribute.name for attribute in parsed.attributes]
        if label == "specification":
            assert names == ["CUSTOMER_ID", "ACCOUNT_NO", "OPEN_DATE", "BRANCH_ID", "BALANCE"]
            continue
        # Nothing of the other table becomes an attribute.
        assert names == ["CUSTOMER_ID", "ACCOUNT_NO", "OPEN_DATE"], label
        if note:
            assert note in parsed.notes, (label, parsed.notes)

    # More than a page footer and header between them, or a page: not a run-on either.
    notes = ["Notes on the feed, which the analyst wrote at length for the data owner."] * 5
    content = _pdf([[_HEADER, *_ROWS], *notes], [_MORE_ROWS])
    assert read_pdf(content, "spec.pdf").text == (
        f"{_tsv([_HEADER, *_ROWS])}\n\n" + "\n".join(notes) + f"\n{_tsv(_MORE_ROWS)}"
    )
    content = _pdf([[_HEADER, *_ROWS]], ["Left blank."], [_MORE_ROWS])
    assert read_pdf(content, "spec.pdf").text == (
        f"{_tsv([_HEADER, *_ROWS])}\n\nLeft blank.\n{_tsv(_MORE_ROWS)}"
    )


def test_an_unruled_table_is_plain_text_that_is_reported_not_read() -> None:
    content = _pdf([_INSTRUCTIONS, [_HEADER, *_ROWS]], ruled=False)
    read = read_pdf(content, "spec.pdf")
    assert read.text.splitlines() == [
        _INSTRUCTIONS,
        "Field Name Data Type Length Description",
        "CUSTOMER_ID VARCHAR 20 Customer identifier",
        "ACCOUNT_NO VARCHAR 16 Account number",
        "OPEN_DATE DATE Date opened",
    ]
    # Nothing is guessed: the specification reader names the line instead.
    parsed = parse_specification(read.text)
    assert parsed.attributes == [] and parsed.flattened == [2]


# -- what is not read


def test_a_file_that_is_not_read_says_why() -> None:
    def note(block: dict[str, Any]) -> tuple[str, ...]:
        read = _read(block)
        assert read.text == "", read
        return read.notes

    image = "was not read: images cannot be read. Ask for its content as text."
    assert note({"type": "image", "base64": "aGk=", "mime_type": "image/png"}) == (
        f"an attached image {image}",
    )
    assert note(_block(b"hi", "spec.png", "image/png")) == (f"spec.png {image}",)
    assert note(_block(b"Source: daily accounts feed.", "notes.txt", "text/plain")) == (
        "notes.txt was not read: only PDFs are. Ask for its content as text.",
    )
    assert note({"type": "file", "mime_type": "text/csv", "base64": "aGk="}) == (
        "an attached file was not read: only PDFs are. Ask for its content as text.",
    )
    assert note(
        {"type": "file", "mime_type": PDF, "url": "https://files.example.com/spec.pdf"}
    ) == ("an attached file came without its content, so it was not read.",)
    too_large = {**_block(b""), "base64": "A" * (documents.MAX_BYTES * 4 // 3 + 4)}
    assert note(too_large) == (
        (
            "spec.pdf is larger than 5 MB, so it was not read: ask for the part that matters as "
            "text, or as a smaller PDF."
        ),
    )
    assert note({**_block(b""), "base64": "abc"}) == (
        "spec.pdf could not be decoded, so it was not read.",
    )

    # A damaged file is logged by the kind of error, never its content.
    logged: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logged.append(record.getMessage())

    sink = _Sink()
    documents.logger.addHandler(sink)
    try:
        damaged = note(_block(b"%PDF-1.4 Source system: CoreBank"))
    finally:
        documents.logger.removeHandler(sink)
    assert damaged == (
        (
            "spec.pdf could not be opened (damaged, or protected by a password), so it was not "
            "read: ask for its content as text."
        ),
    )
    assert len(logged) == 1 and logged[0].startswith("Could not read an attached PDF: ")
    assert "CoreBank" not in logged[0]

    # A scan has pages but no text.
    assert _read(_block(_pdf([], []))) == Read(
        "spec.pdf",
        pages=2,
        notes=(
            (
                "spec.pdf has no text to read (a scanned document?): ask for its content as text, "
                "or a PDF with selectable text."
            ),
        ),
    )


def test_reading_stops_at_the_page_character_and_time_limits() -> None:
    three_pages = _pdf(["Page one."], ["Page two."], ["Page three."])
    rest = "ask for the rest as text or in another message."
    with _patched(MAX_PAGES=2):
        assert read_attachment(_block(three_pages)) == Read(
            "spec.pdf",
            "Page one.\nPage two.",
            pages=3,
            notes=(f"spec.pdf: of its 3 pages only pages 1-2 were read; {rest}",),
        )
    # Out of time, the first page is still read.
    with _patched(MAX_SECONDS=0):
        assert read_attachment(_block(three_pages)) == Read(
            "spec.pdf",
            "Page one.",
            pages=3,
            notes=(f"spec.pdf: of its 3 pages only page 1 was read; {rest}",),
        )

    # Cut at the end of the last line that fits; inside a line if none does.
    content = _pdf([_INSTRUCTIONS, "Specification follows."])
    with _patched(MAX_CHARS=60):
        assert read_attachment(_block(content)) == Read(
            "spec.pdf",
            _INSTRUCTIONS,
            pages=1,
            notes=(f"spec.pdf: only its first 53 characters were read; {rest}",),
        )
    with _patched(MAX_CHARS=10):
        assert read_attachment(_block(content)).text == "Source sys"


def test_a_messages_attachments_add_only_so_much_text() -> None:
    blocks = [
        _block(_pdf(["Source system: CoreBank."]), "a.pdf"),
        _block(_pdf(["Specification follows. " * 3]), "b.pdf"),
        _block(_pdf(["Source: daily accounts feed."]), "c.pdf"),
    ]
    # a.pdf and c.pdf fit; b.pdf, between them, does not.
    with _patched(MAX_MESSAGE_CHARS=60):
        assert attachments_text(blocks) == (
            [
                '<document name="a.pdf" pages="1">\nSource system: CoreBank.\n</document>',
                '<document name="c.pdf" pages="1">\nSource: daily accounts feed.\n</document>',
            ],
            [
                (
                    "b.pdf was not read: this message's attachments hold more text than can be "
                    "read at once. Ask for it in another message."
                )
            ],
        )


# -- shapes, the cache, and the message


def test_each_shape_a_file_is_sent_in_is_read_alike() -> None:
    content = _pdf([_INSTRUCTIONS])
    data = base64.b64encode(content).decode()
    shapes = {
        "frontend": _block(content, "a.pdf"),
        "source type": {
            "type": "file",
            "source_type": "base64",
            "data": data,
            "mime_type": PDF,
            "metadata": {"filename": "a.pdf"},
        },
        "data URL": {
            "type": "file",
            "file": {"filename": "a.pdf", "file_data": f"data:{PDF};base64,{data}"},
        },
        "camel case": {"type": "file", "base64": data, "mimeType": PDF, "name": "a.pdf"},
    }
    for label, block in shapes.items():
        assert _read(block) == Read("a.pdf", _INSTRUCTIONS, pages=1), label


def test_a_file_is_read_once_for_every_task_of_a_turn() -> None:
    calls: list[str] = []

    def counted(content: bytes, name: str) -> Read:
        calls.append(name)
        return Read(name, "text", pages=1)

    a, b = _block(_pdf(["A"]), "a.pdf"), _block(_pdf(["B"]), "b.pdf")
    with _patched(read_pdf=counted):
        assert read_attachment(a) == read_attachment(a) == Read("a.pdf", "text", pages=1)
        assert calls == ["a.pdf"]
        # The same content under another name: its notes name it, so it is read again.
        read_attachment({**a, "extras": {"filename": "copy.pdf"}})
        assert calls == ["a.pdf", "copy.pdf"]
    with _patched(read_pdf=counted, _CACHE_SIZE=1):
        calls.clear()
        for block in (a, b, a):
            read_attachment(block)
        assert calls == ["a.pdf", "b.pdf", "a.pdf"]


def test_a_messages_text_comes_first_then_its_documents() -> None:
    content = [
        {"type": "text", "text": "Here is the specification."},
        _block(_pdf([_INSTRUCTIONS]), "a.pdf"),
        {"type": "text", "text": "  "},
        {
            "type": "image",
            "base64": "aGk=",
            "mime_type": "image/png",
            "extras": {"filename": "s.png"},
        },
        _block(_pdf(["Specification follows."]), 'x"<y>.pdf'),
        "And the rest.",
    ]
    with _patched():
        text, notes = read_message(content)
    # The name may not end the tag or open another one.
    assert text == (
        "Here is the specification.\n\nAnd the rest.\n\n"
        f'<document name="a.pdf" pages="1">\n{_INSTRUCTIONS}\n</document>\n\n'
        '<document name="x\'(y).pdf" pages="1">\nSpecification follows.\n</document>'
    )
    assert notes == ["s.png was not read: images cannot be read. Ask for its content as text."]
    # Which file a line of the message comes from, for a refusal to name it.
    lines = text.splitlines()
    assert [document_at(text, number) for number in range(1, len(lines) + 2)] == [
        None,
        None,
        None,
        None,
        None,
        "a.pdf",
        None,
        None,
        None,
        "x'(y).pdf",
        None,
        None,
    ]
    assert message_parts(content)[0] == ["Here is the specification.", "And the rest."]

    assert read_message("Hi.") == ("Hi.", [])
    assert read_message("  ") == ("", [])
    assert read_message([]) == ("", [])


def test_a_messages_parts_are_told_apart_and_its_files_left_out() -> None:
    text = (
        "Here is the specification.\n\n"
        '<user_skill name="Fast STTM">\nFor this message, use my skill "Fast STTM".\n'
        "Carry every attribute.\n</user_skill>\n\n"
        '<document name="a.pdf" pages="2">\nConfidential feed.\n</document>'
    )

    # Lines from 0; the tags are no part's.
    assert message_layout(text) == [
        Part("text", "", 0, 1),
        Part("user_skill", "Fast STTM", 3, 4),
        Part("text", "", 6, 6),
        Part("document", "a.pdf", 8, 8),
    ]
    assert without_documents(text) == (
        'Here is the specification.\n\nFor this message, use my skill "Fast STTM".\n'
        "Carry every attribute.\n"
    )
    # A file's text that is not closed runs to the end; a tag inside a line is text.
    cut = 'See <document name="b.pdf"> below.\n<document name="b.pdf" pages="1">\nMore text'
    assert message_layout(cut) == [Part("text", "", 0, 0), Part("document", "b.pdf", 2, 2)]
    assert without_documents(cut) == 'See <document name="b.pdf"> below.'
    assert message_layout("") == [] and without_documents("") == ""


def _main() -> int:
    checks = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - standalone runner reports all
            failures += 1
            print(f"FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {check.__name__}")
    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
