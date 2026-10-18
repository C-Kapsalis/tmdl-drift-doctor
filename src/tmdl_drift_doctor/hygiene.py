"""Override hygiene: a declaration that protects nothing is a finding.

A file exemption reads like protection whether or not it gates anything.
It stops gating when its path has a typo, when a rename leaves it pointing
at nothing, or when the model's copy became identical to the template's
again. Each of those is reported, naming the model and the path.
"""

from __future__ import annotations

from pathlib import Path

from .fleet import Fleet


def _matches(root: Path, pattern: str) -> list:
    if any(ch in pattern for ch in "*?["):
        return sorted(p for p in root.glob(pattern) if p.is_file())
    target = root / pattern
    return [target] if target.is_file() else []


def file_exemption_problems(fleet: Fleet) -> list:
    out = []
    for name in fleet.primary_models:
        for i, pattern in enumerate(fleet.exempt_patterns(name)):
            pattern = (pattern or "").strip()
            if not pattern:
                out.append(f"{name}: overrides.files[{i}]: empty path (it "
                           f"protects nothing)")
                continue
            in_template = _matches(fleet.template_root, pattern)
            in_model = _matches(fleet.model_root(name), pattern)
            if not in_template:
                out.append(f"{name}: {pattern}: does not exist in the "
                           f"template")
            if not in_model:
                out.append(f"{name}: {pattern}: missing in the model")
            if in_template and in_model:
                rel = {p.relative_to(fleet.template_root): p
                       for p in in_template}
                for mp in in_model:
                    tp = rel.get(mp.relative_to(fleet.model_root(name)))
                    if tp is not None and tp.read_bytes() == mp.read_bytes():
                        out.append(
                            f"{name}: {mp.relative_to(fleet.model_root(name)).as_posix()}"
                            f": byte-identical to the template's copy (the "
                            f"exemption protects nothing; drop it)")
    return out
