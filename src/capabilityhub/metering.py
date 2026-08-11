"""Deterministic payload accounting with explicit estimator identity."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Protocol

from capabilityhub.models import JsonValue


def canonical_json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TokenEstimator(Protocol):
    @property
    def name(self) -> str: ...

    def count(self, text: str) -> int: ...


@dataclass(frozen=True, slots=True)
class Utf8Div4Estimator:
    """Portable deterministic estimate; never presented as provider billing tokens."""

    @property
    def name(self) -> str:
        return "utf8-bytes-div-4-v1"

    def count(self, text: str) -> int:
        byte_count = len(text.replace("\r\n", "\n").encode("utf-8"))
        return math.ceil(byte_count / 4)


@dataclass(frozen=True, slots=True)
class PayloadMeasurement:
    utf8_bytes: int
    portable_tokens: int
    estimator: str


def measure_text(text: str, estimator: TokenEstimator | None = None) -> PayloadMeasurement:
    normalized = text.replace("\r\n", "\n")
    selected = estimator or Utf8Div4Estimator()
    return PayloadMeasurement(
        utf8_bytes=len(normalized.encode("utf-8")),
        portable_tokens=selected.count(normalized),
        estimator=selected.name,
    )
