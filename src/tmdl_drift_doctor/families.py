"""Model families: named sets of fleet models that share an architecture.

The growth family is recognised by the model folder's suffix (several are
configured on purpose: a growth model whose folder is named otherwise must
not drop out silently) or by an explicit ``family: growth`` on its entry.
The template is never a member.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from .fleet import DEFAULT_GROWTH_SUFFIXES, Fleet

GROWTH_SUFFIXES = DEFAULT_GROWTH_SUFFIXES


def is_growth_model_path(path, suffixes=GROWTH_SUFFIXES) -> bool:
    """Does a model path name a growth-architecture model? An empty path
    never does."""
    text = str(path or "").replace("\\", "/").rstrip("/")
    if not text:
        return False
    return PurePosixPath(text).name in tuple(suffixes)


def members(fleet: Fleet, family: str) -> list:
    """Sorted ids of the fleet models in `family` (template excluded)."""
    out = []
    for name, path in fleet.models.items():
        if path == fleet.template:
            continue
        declared = fleet.families.get(name)
        if declared == family or (family == "growth" and is_growth_model_path(
                path.as_posix(), fleet.growth_suffixes)):
            out.append(name)
    return sorted(out)
