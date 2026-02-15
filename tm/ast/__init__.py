from .canonical import SET_LIKE_PATHS, canonical_dumps, canonicalize, is_set_like_field

try:
    from .validator import AstValidationIssue, SUPPORTED_CANONICAL_KINDS, validate_canonical_ast
except ModuleNotFoundError as exc:
    from dataclasses import dataclass
    from typing import Any, Mapping, Sequence

    _VALIDATOR_IMPORT_ERROR = exc
    SUPPORTED_CANONICAL_KINDS: tuple[str, ...] = tuple()

    @dataclass(frozen=True)
    class AstValidationIssue:  # pragma: no cover - fallback for missing optional validator deps
        json_path: str
        message: str

    def validate_canonical_ast(payload: Mapping[str, Any]) -> Sequence[AstValidationIssue]:
        raise ModuleNotFoundError(
            "canonical AST validation dependencies are missing; install extras required by tm.ast.validator"
        ) from _VALIDATOR_IMPORT_ERROR


__all__ = [
    "AstValidationIssue",
    "SUPPORTED_CANONICAL_KINDS",
    "SET_LIKE_PATHS",
    "canonical_dumps",
    "canonicalize",
    "is_set_like_field",
    "validate_canonical_ast",
]
