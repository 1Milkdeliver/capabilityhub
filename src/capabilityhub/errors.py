"""Stable, model-safe error contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    INPUT = "input"
    REFERENCE = "reference"
    POLICY = "policy"
    APPROVAL = "approval"
    BUDGET = "budget"
    CONFLICT = "conflict"
    PROVIDER = "provider"
    DEPENDENCY = "dependency"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERNAL = "internal"


@dataclass(slots=True)
class CapabilityHubError(Exception):
    code: str
    category: ErrorCategory
    safe_message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.safe_message

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "category": self.category.value,
                "retryable": self.retryable,
                "safe_message": self.safe_message,
                "details": self.details,
            }
        }

