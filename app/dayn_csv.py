"""Parser and exporter for Cisco Catalyst Center Day-N template import CSVs.

The format is a sequence of header/data line pairs. Every header line starts
with the literal cell ``Project Name:Template Name`` and defines the variable
columns for exactly the one data line that follows it. All fields are
double-quoted; line endings are mixed in real exports (headers ``\\n``, data
rows ``\\r\\n``) and must round-trip byte-exactly for unchanged rows.

Both functions are pure: bytes in -> model, model -> bytes.
"""

import csv
import io

from app.models import CsvDocument, TemplateBlock

HEADER_MARKER = "Project Name:Template Name"


class DayNCsvError(Exception):
    """Raised when an uploaded file is not a valid Day-N template CSV."""


def _split_lines(text: str) -> list[tuple[str, str]]:
    """Split text into (line, ending) pairs, accepting \\n and \\r\\n."""
    lines: list[tuple[str, str]] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            lines.append((text[start:i], "\n"))
            i += 1
            start = i
        elif ch == "\r" and i + 1 < n and text[i + 1] == "\n":
            lines.append((text[start:i], "\r\n"))
            i += 2
            start = i
        elif ch == "\r":
            lines.append((text[start:i], "\r"))
            i += 1
            start = i
        else:
            i += 1
    if start < n:
        lines.append((text[start:], ""))
    return lines


def _parse_cells(line: str, line_no: int) -> list[str]:
    try:
        rows = list(csv.reader(io.StringIO(line)))
    except csv.Error as exc:
        raise DayNCsvError(f"Line {line_no}: not parseable as CSV: {exc}") from exc
    if not rows:
        raise DayNCsvError(f"Line {line_no}: empty row where CSV cells were expected")
    return rows[0]


def parse(data: bytes) -> CsvDocument:
    """Parse raw upload bytes into a :class:`CsvDocument`.

    Raises :class:`DayNCsvError` on empty input, a wrong first cell, a data
    row without a preceding header, or a header without a data row.
    """
    if not data.strip():
        raise DayNCsvError("The uploaded file is empty.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DayNCsvError("The uploaded file is not valid UTF-8.") from exc

    lines = [(line, ending) for line, ending in _split_lines(text) if line.strip()]

    blocks: list[TemplateBlock] = []
    idx = 0
    while idx < len(lines):
        header_line, header_ending = lines[idx]
        line_no = idx + 1
        header_cells = _parse_cells(header_line, line_no)
        if not header_cells or header_cells[0] != HEADER_MARKER:
            raise DayNCsvError(
                f"Line {line_no}: expected a header row starting with "
                f"'{HEADER_MARKER}', got '{header_cells[0] if header_cells else ''}'."
            )
        if len(header_cells) < 2 or header_cells[1] != "Device Name":
            raise DayNCsvError(f"Line {line_no}: header row is missing the 'Device Name' column.")
        if idx + 1 >= len(lines):
            raise DayNCsvError(f"Line {line_no}: header row has no data row following it.")

        data_line, data_ending = lines[idx + 1]
        data_cells = _parse_cells(data_line, line_no + 1)
        if data_cells and data_cells[0] == HEADER_MARKER:
            raise DayNCsvError(f"Line {line_no}: header row has no data row following it.")
        if len(data_cells) < 2:
            raise DayNCsvError(f"Line {line_no + 1}: data row has no device name.")
        if len(data_cells) > len(header_cells):
            raise DayNCsvError(
                f"Line {line_no + 1}: data row has more cells ({len(data_cells)}) "
                f"than its header ({len(header_cells)})."
            )

        columns = header_cells[2:]
        values = data_cells[2:]
        values += [""] * (len(columns) - len(values))
        variables = dict(zip(columns, values, strict=True))
        blocks.append(
            TemplateBlock(
                template=data_cells[0],
                device_name=data_cells[1],
                columns=columns,
                variables=variables,
                raw_header=header_line,
                raw_data=data_line,
                header_ending=header_ending,
                data_ending=data_ending,
                original_variables=dict(variables),
            )
        )
        idx += 2

    return CsvDocument(blocks=blocks)


def _quote_row(cells: list[str]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, quoting=csv.QUOTE_ALL, lineterminator="")
    writer.writerow(cells)
    return out.getvalue()


def export(doc: CsvDocument) -> bytes:
    """Serialize a document back to bytes.

    Unchanged rows are emitted from their raw lines (byte-exact); rows whose
    variables changed are rebuilt with all fields double-quoted, keeping the
    row's original line ending.
    """
    parts: list[str] = []
    for block in doc.blocks:
        parts.append(block.raw_header + block.header_ending)
        if block.variables == block.original_variables:
            parts.append(block.raw_data + block.data_ending)
        else:
            cells = [block.template, block.device_name]
            cells += [block.variables.get(col, "") for col in block.columns]
            parts.append(_quote_row(cells) + block.data_ending)
    return "".join(parts).encode("utf-8")
