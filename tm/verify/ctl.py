from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Set

from .adapter import TraceMindAdapter
from .explorer import ExplorationResult
from .state import State

_TOKEN = re.compile(r"\s*(EX|EF|EG|AF|AG|AND|OR|NOT|\|\||&&|!|\(|\)|[A-Za-z_][A-Za-z0-9_\-\.\[\]\*]*|\S)")


class Expr:
    pass


@dataclass
class Predicate(Expr):
    name: str
    value: Optional[str]


@dataclass
class Not(Expr):
    child: Expr


@dataclass
class And(Expr):
    left: Expr
    right: Expr


@dataclass
class Or(Expr):
    left: Expr
    right: Expr


@dataclass
class Ctl(Expr):
    op: str
    child: Expr


def _tokenize(expr: str) -> List[str]:
    tokens: List[str] = []
    idx = 0
    while idx < len(expr):
        m = _TOKEN.match(expr, idx)
        if not m:
            break
        tok = m.group(1)
        tokens.append(tok)
        idx = m.end()
    return tokens


def parse_expr(expr: str) -> Expr:
    tokens = _tokenize(expr)
    pos = 0

    def peek() -> Optional[str]:
        return tokens[pos] if pos < len(tokens) else None

    def pop() -> str:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        return tok

    def parse_predicate() -> Expr:
        head = pop()
        value: Optional[str] = None
        if head.lower() == "peer":
            if peek() != "(":
                raise ValueError("peer() requires '('")
            pop()
            agent_id = pop()
            if peek() == ",":
                pop()
            inner = parse_predicate()
            if peek() == ")":
                pop()
            if not isinstance(inner, Predicate):
                raise ValueError("peer() second argument must be a predicate call")
            if inner.value is None:
                raise ValueError("peer() inner predicate requires an argument")
            if inner.name.lower() not in ("has", "pending", "done"):
                raise ValueError("peer() supports has/pending/done inner predicates only")
            return Predicate(name=inner.name, value=f"{agent_id}.{inner.value}")
        if peek() == "(":
            pop()
            parts: List[str] = []
            while peek() not in (")", None):
                parts.append(pop())
            if peek() != ")":
                raise ValueError("missing ')' in predicate")
            pop()
            value = "".join(parts).strip() or None
        if head.lower() == "terminal":
            return Predicate(name="Terminal", value=None)
        return Predicate(name=head, value=value)

    def parse_unary() -> Expr:
        tok = peek()
        if tok is None:
            raise ValueError("unexpected end of expression")
        if tok in ("!", "NOT"):
            pop()
            return Not(parse_unary())
        if tok in {"EX", "EF", "EG", "AF", "AG"}:
            op = pop()
            return Ctl(op=op, child=parse_unary())
        if tok == "(":
            pop()
            node = parse_or()
            if peek() != ")":
                raise ValueError("missing ')'")
            pop()
            return node
        return parse_predicate()

    def parse_and() -> Expr:
        node = parse_unary()
        while True:
            tok = peek()
            if tok not in ("&&", "AND"):
                break
            pop()
            node = And(left=node, right=parse_unary())
        return node

    def parse_or() -> Expr:
        node = parse_and()
        while True:
            tok = peek()
            if tok not in ("||", "OR"):
                break
            pop()
            node = Or(left=node, right=parse_and())
        return node

    node = parse_or()
    if pos != len(tokens):
        raise ValueError(f"unexpected token '{tokens[pos]}'")
    return node


def _parse_predicate_call(head: str, tokens: List[str], pos: int) -> tuple[Predicate, int]:
    value_parts: List[str] = []
    if pos >= len(tokens) or tokens[pos] != "(":
        return Predicate(name=head, value=None), pos
    pos += 1
    while pos < len(tokens) and tokens[pos] != ")":
        value_parts.append(tokens[pos])
        pos += 1
    if pos >= len(tokens) or tokens[pos] != ")":
        raise ValueError("missing ')' in predicate")
    pos += 1
    val = "".join(value_parts).strip() if value_parts else None
    return Predicate(name=head, value=val), pos


def parse_predicate_expr(expr: str) -> Predicate:
    tokens = _tokenize(expr)
    if not tokens:
        raise ValueError("empty predicate")
    if tokens[0].lower() == "peer":
        node = parse_expr(expr)
        if not isinstance(node, Predicate):
            raise ValueError("peer expression must desugar to a predicate")
        return node
    pred, pos = _parse_predicate_call(tokens[0], tokens, 1)
    if pos != len(tokens):
        raise ValueError("trailing tokens in predicate")
    return pred


def _eval_predicate(pred: Predicate, state: State, adapter: TraceMindAdapter) -> bool:
    name = pred.name
    if name.lower() == "terminal":
        return not adapter.enabled_steps(state)
    if pred.value is None:
        return False
    if name.lower() == "has":
        return pred.value in state.store
    if name.lower() == "pending":
        return pred.value in state.pending
    if name.lower() == "done":
        return pred.value in state.done
    return False


def has_ctl_nodes(expr: Expr) -> bool:
    if isinstance(expr, Ctl):
        return True
    if isinstance(expr, (Predicate)):
        return False
    if isinstance(expr, Not):
        return has_ctl_nodes(expr.child)
    if isinstance(expr, (And, Or)):
        return has_ctl_nodes(expr.left) or has_ctl_nodes(expr.right)
    return False


def eval_state_expr(expr: Expr, state: State, adapter: TraceMindAdapter) -> bool:
    if isinstance(expr, Predicate):
        return _eval_predicate(expr, state, adapter)
    if isinstance(expr, Not):
        return not eval_state_expr(expr.child, state, adapter)
    if isinstance(expr, And):
        return eval_state_expr(expr.left, state, adapter) and eval_state_expr(expr.right, state, adapter)
    if isinstance(expr, Or):
        return eval_state_expr(expr.left, state, adapter) or eval_state_expr(expr.right, state, adapter)
    if isinstance(expr, Ctl):
        raise ValueError("CTL operator not allowed in state-only evaluation")
    raise TypeError(f"Unsupported expression node: {expr}")


def _sat(expr: Expr, model: ExplorationResult, adapter: TraceMindAdapter) -> Set[int]:
    if isinstance(expr, Predicate):
        return {i for i, st in enumerate(model.states) if _eval_predicate(expr, st, adapter)}
    if isinstance(expr, Not):
        all_states = set(range(len(model.states)))
        return all_states - _sat(expr.child, model, adapter)
    if isinstance(expr, And):
        return _sat(expr.left, model, adapter) & _sat(expr.right, model, adapter)
    if isinstance(expr, Or):
        return _sat(expr.left, model, adapter) | _sat(expr.right, model, adapter)
    if isinstance(expr, Ctl):
        child_sat = _sat(expr.child, model, adapter)
        if expr.op == "EX":
            return {sid for sid, succs in model.edges.items() if any(nxt in child_sat for nxt in succs)}
        if expr.op == "EF":
            sat = set(child_sat)
            changed = True
            while changed:
                changed = False
                for sid, succs in model.edges.items():
                    if sid in sat:
                        continue
                    if any(nxt in sat for nxt in succs):
                        sat.add(sid)
                        changed = True
            return sat
        if expr.op == "AF":
            sat = set(child_sat)
            changed = True
            while changed:
                changed = False
                for sid, succs in model.edges.items():
                    if sid in sat:
                        continue
                    if succs and all(nxt in sat for nxt in succs):
                        sat.add(sid)
                        changed = True
            # terminals that already satisfy child are included; those that do not stay out
            for sid in range(len(model.states)):
                if sid not in model.edges and sid in child_sat:
                    sat.add(sid)
            return sat
        if expr.op == "EG":
            # EG p == not AF not p
            return set(range(len(model.states))) - _sat(Ctl(op="AF", child=Not(expr.child)), model, adapter)
        if expr.op == "AG":
            # AG p == not EF not p
            return set(range(len(model.states))) - _sat(Ctl(op="EF", child=Not(expr.child)), model, adapter)
    raise TypeError(f"Unsupported expression node: {expr}")


def check_ctl(expr: Expr, model: ExplorationResult, adapter: TraceMindAdapter) -> Set[int]:
    return _sat(expr, model, adapter)
