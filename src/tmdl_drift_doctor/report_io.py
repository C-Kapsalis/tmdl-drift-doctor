"""Reading a saved ``detect --json`` report.

A report saved on Windows is not always plain UTF-8: Windows PowerShell 5.1
writes ``detect --json > report.json`` as UTF-16 with a byte-order mark, and
``Out-File -Encoding utf8`` writes UTF-8 with one. Every command that reads
a saved report reads it through here, so each of them accepts UTF-8 (with or
without a byte-order mark) and UTF-16 (told apart by its byte-order mark).
"""

from __future__ import annotations

import codecs
from pathlib import Path


class ReportEncodingError(ValueError):
    """The report's bytes are not text in an encoding the tool reads."""


def read_report_text(path) -> str:
    """The text of the report at `path`, whatever supported encoding it is
    in. Raises ``OSError`` when the file cannot be read and
    ``ReportEncodingError`` when its bytes are not supported text."""
    raw = Path(path).read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        encoding, body = "UTF-8 with a byte-order mark", raw[3:]
        codec = "utf-8"
    elif raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding, body, codec = "UTF-16", raw, "utf-16"
    else:
        encoding, body, codec = "UTF-8", raw, "utf-8"
    try:
        return body.decode(codec)
    except UnicodeDecodeError as e:
        raise ReportEncodingError(
            f"the file is not valid {encoding} text ({e}); save the report "
            f"as UTF-8 (with or without a byte-order mark) or as UTF-16 with "
            f"a byte-order mark") from e
