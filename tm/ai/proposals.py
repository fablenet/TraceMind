from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Sequence

Operation = Literal["set", "remove"]


@dataclass(frozen=True)
class Change:
    """Describe an atomic modification to a policy document."""

    path: str
    value: Any | None = None
    op: Operation = "set"
    note: str | None = None

    def apply(self, root: Dict[str, Any]) -> None:
        """Apply the change to ``root`` in-place."""

        parts = [p for p in self.path.split(".") if p]
        if not parts:
            raise ValueError("Change path cannot be empty")
        parent = root
        for segment in parts[:-1]:
            if segment not in parent or not isinstance(parent[segment], dict):
                parent[segment] = {}
            parent = parent[segment]
        leaf = parts[-1]
        if self.op == "remove":
            parent.pop(leaf, None)
        else:
            parent[leaf] = self.value


@dataclass(frozen=True)
class Proposal:
    """Bundle of policy changes accompanied by contextual metadata."""

    proposal_id: str
    title: str
    summary: str
    changes: Sequence[Change]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.proposal_id,
            "title": self.title,
            "summary": self.summary,
            "created_at": self.created_at.isoformat(),
            "metadata": dict(self.metadata),
            "changes": [
                {
                    "path": change.path,
                    "value": change.value,
                    "op": change.op,
                    "note": change.note,
                }
                for change in self.changes
            ],
        }
