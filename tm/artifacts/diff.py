from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Sequence

from tm.ast.canonical import canonical_dumps

from .models import Artifact


@dataclass
class DiffReport:
    current_artifact_id: str
    previous_artifact_id: str
    canonical_current: str
    canonical_previous: str
    diff_lines: Sequence[str]

    def human_summary(self) -> str:
        return (
            f"artifact {self.current_artifact_id} differs from {self.previous_artifact_id} "
            "in canonical body representation"
        )

    def machine_readable(self) -> dict[str, object]:
        return {
            "current_artifact_id": self.current_artifact_id,
            "previous_artifact_id": self.previous_artifact_id,
            "diff": list(self.diff_lines),
            "canonical_current": self.canonical_current,
            "canonical_previous": self.canonical_previous,
        }


def diff_artifacts(current: Artifact, previous: Artifact) -> DiffReport:
    current_canonical = canonical_dumps(current.body_raw)
    previous_canonical = canonical_dumps(previous.body_raw)
    diff_lines = list(
        difflib.unified_diff(
            previous_canonical.splitlines(),
            current_canonical.splitlines(),
            fromfile=previous.envelope.artifact_id,
            tofile=current.envelope.artifact_id,
            lineterm="",
        )
    )
    return DiffReport(
        current_artifact_id=current.envelope.artifact_id,
        previous_artifact_id=previous.envelope.artifact_id,
        canonical_current=current_canonical,
        canonical_previous=previous_canonical,
        diff_lines=diff_lines,
    )
