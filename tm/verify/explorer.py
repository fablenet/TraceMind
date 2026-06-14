from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, List, Tuple

from .adapter import TraceMindAdapter
from .state import State


@dataclass
class ExplorationResult:
    states: List[State]
    edges: Dict[int, List[int]]
    predecessors: Dict[int, Tuple[int, str]]
    deadlocks: List[int]
    hash_mode: str
    max_depth: int

    def path_to(self, target: int) -> List[int]:
        if target >= len(self.states):
            return []
        path: List[int] = [target]
        cur = target
        seen = {cur}
        while cur in self.predecessors:
            prev, _ = self.predecessors[cur]
            # Stop on cycles (e.g. self-loops set predecessors[s] = (s, ...)),
            # otherwise back-tracking the predecessor chain never terminates.
            if prev in seen:
                break
            path.append(prev)
            seen.add(prev)
            cur = prev
        path.reverse()
        return path


class Explorer:
    def __init__(self, adapter: TraceMindAdapter):
        self.adapter = adapter

    def run(self, *, max_depth: int = 8, hash_mode: str = "full") -> ExplorationResult:
        root = self.adapter.initial_state()
        states: List[State] = [root]
        edges: Dict[int, List[int]] = defaultdict(list)
        predecessors: Dict[int, Tuple[int, str]] = {}
        deadlocks: List[int] = []
        seen: Dict[str, int] = {root.stable_hash(hash_mode): 0}
        depths: Dict[int, int] = {0: 0}

        q: deque[Tuple[int, State]] = deque()
        q.append((0, root))
        while q:
            sid, st = q.popleft()
            depth = depths.get(sid, 0)
            succ = [] if depth >= max_depth else self.adapter.successors(st)
            if not succ and self.adapter.is_deadlocked(st):
                deadlocks.append(sid)
            for label, nxt in succ:
                h = nxt.stable_hash(hash_mode)
                if h in seen:
                    nid = seen[h]
                else:
                    nid = len(states)
                    states.append(nxt)
                    seen[h] = nid
                    depths[nid] = depth + 1
                    q.append((nid, nxt))
                edges[sid].append(nid)
                predecessors.setdefault(nid, (sid, label))

        return ExplorationResult(
            states=states,
            edges={k: v for k, v in edges.items()},
            predecessors=predecessors,
            deadlocks=deadlocks,
            hash_mode=hash_mode,
            max_depth=max_depth,
        )
