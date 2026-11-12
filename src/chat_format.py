"""
Chat answer formatting helpers.

The model sometimes puts a footnote inside a Markdown table as a last row
whose only filled cell is the first one:

    | Name | ID | Email |
    |------|----|-------|
    | Ravi | 1  | r@x.ai|
    | *Note: Project info comes from the employee record.* | | |

Browsers size a table column to its widest cell, so that single long note
stretches the Name column across the screen and forces HR to scroll
sideways. ``tidy_markdown_tables`` moves such trailing note rows out of the
table and places them as a normal line underneath it. Anything else is
returned unchanged.
"""

from __future__ import annotations

import re

_SEPARATOR_CELL = re.compile(r"^:?-+:?$")
_NOTE_START = re.compile(
    r"^[\s*_`~]*(?:(?:note|notes|footnote|source|sources|legend)\b|[†‡])",
    re.IGNORECASE,
)
_LONG_NOTE_CHARS = 40


def _split_row(line: str) -> list[str] | None:
    """Cells of a Markdown table row, or None if the line is not a row."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    body = stripped[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", body)]


def _is_separator(cells: list[str] | None) -> bool:
    return bool(cells) and all(_SEPARATOR_CELL.match(c) for c in cells if c != "") \
        and any(c != "" for c in cells)


def _is_note_row(cells: list[str], column_count: int) -> bool:
    """First cell filled, every other cell empty, and the text reads as a note."""
    if column_count < 2 or not cells or not cells[0]:
        return False
    if any(cell for cell in cells[1:]):
        return False
    first = cells[0]
    return bool(_NOTE_START.match(first)) or len(first) > _LONG_NOTE_CHARS


def tidy_markdown_tables(text: str) -> str:
    """Move trailing note rows out of Markdown tables; leave all else as is."""
    if not text or "|" not in text:
        return text

    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.strip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue

        header = _split_row(line) if not in_fence else None
        is_table_start = (
            header is not None
            and i + 1 < len(lines)
            and _is_separator(_split_row(lines[i + 1]))
        )
        if not is_table_start:
            out.append(line)
            i += 1
            continue

        # Collect the whole table block (consecutive pipe rows).
        block = [line, lines[i + 1]]
        j = i + 2
        while j < len(lines) and _split_row(lines[j]) is not None:
            block.append(lines[j])
            j += 1

        column_count = len(header)
        notes: list[str] = []
        # Peel note rows off the END of the table only (data rows keep order).
        while len(block) > 2:
            cells = _split_row(block[-1])
            if cells is not None and _is_note_row(cells, column_count):
                notes.insert(0, cells[0])
                block.pop()
            else:
                break

        out.extend(block)
        if notes:
            out.append("")
            out.extend(notes if len(notes) == 1 else
                       [n + "  " if k < len(notes) - 1 else n
                        for k, n in enumerate(notes)])
        i = j

    return "\n".join(out)


def tidy_stream(chunks):
    """
    Wrap the agent's answer stream. The agent produces the complete answer
    before it starts yielding, so buffering costs nothing. If tidying changes
    nothing, the original chunks are yielded untouched.
    """
    buffered = list(chunks)
    original = "".join(buffered)
    tidied = tidy_markdown_tables(original)
    if tidied == original:
        yield from buffered
        return

    words = tidied.split(" ")
    piece = ""
    for index, word in enumerate(words):
        piece += (" " if piece else "") + word
        if len(piece) >= 60 and index < len(words) - 1:
            yield piece + " "
            piece = ""
    if piece:
        yield piece
