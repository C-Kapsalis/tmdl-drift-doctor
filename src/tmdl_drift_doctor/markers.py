"""Client extension markers: derived-only text fenced by comment markers.

A model may extend a template-owned expression with its own lines wrapped
in ``-- BEGIN <tag>`` / ``-- END <tag>`` comments. The fenced lines are cut
out before comparing the model with the template, and the template must
never carry the tag at all.
"""

from __future__ import annotations

import re


def _marker(word: str, tag: str):
    return re.compile(rf"^[ \t]*--[ \t]*{word}[ \t]+{re.escape(tag)}\b")


def marker_counts(text: str, tag: str) -> tuple:
    """(BEGIN count, END count) of `tag` markers in `text`."""
    begin, end = _marker("BEGIN", tag), _marker("END", tag)
    lines = text.split("\n")
    return (sum(1 for ln in lines if begin.match(ln)),
            sum(1 for ln in lines if end.match(ln)))


def strip_marker_blocks(text: str, tag: str) -> str:
    """`text` with every BEGIN..END block of `tag` removed (marker lines
    included); every other line is kept byte for byte. An unclosed BEGIN
    keeps its lines, so the imbalance stays visible."""
    begin, end = _marker("BEGIN", tag), _marker("END", tag)
    lines = text.split("\n")
    kept, pending = [], None
    for line in lines:
        if pending is None:
            if begin.match(line):
                pending = [line]
            else:
                kept.append(line)
        else:
            pending.append(line)
            if end.match(line):
                pending = None
    if pending:
        kept.extend(pending)
    return "\n".join(kept)
