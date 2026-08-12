"""Transport-neutral, acknowledgement-based context removal coordination."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from secrets import token_hex
from typing import cast

from capabilityhub.errors import CapabilityHubError, ErrorCategory
from capabilityhub.models import JsonValue
from capabilityhub.protocol import AdapterKind
from capabilityhub.tenancy import SqliteScopedState, TenantScope

CONTEXT_REMOVAL = "context.removal-ack-v1"
_NAMESPACE = "context-removal"
_KEY = "state"


class RemovalStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"


@dataclass(frozen=True, slots=True)
class RemovalNegotiation:
    adapter: AdapterKind
    supported: bool
    feature: str = CONTEXT_REMOVAL
    reason_code: str = "context_removal_supported"


@dataclass(frozen=True, slots=True)
class RemovalInstruction:
    instruction_id: str
    target: str
    generation: int
    attempts: int
    status: RemovalStatus
    acknowledged: bool

    @property
    def confirmed(self) -> bool:
        return self.status is RemovalStatus.CONFIRMED and self.acknowledged

    def as_dict(self) -> dict[str, JsonValue]:
        return _public(self)


class ContextRemovalCoordinator:
    """Persist instructions; never infer deletion without a positive client ack."""

    def __init__(
        self,
        state: SqliteScopedState,
        scope: TenantScope,
        *,
        adapter: AdapterKind,
        client_features: Iterable[str],
        record_limit: int = 500,
    ) -> None:
        if not 1 <= record_limit <= 10_000:
            raise ValueError("record_limit must be from 1 to 10000")
        self._state = state
        self._scope = scope
        self._adapter = adapter
        self._supported = CONTEXT_REMOVAL in frozenset(client_features)
        self._record_limit = record_limit

    @property
    def negotiation(self) -> RemovalNegotiation:
        return RemovalNegotiation(
            self._adapter,
            self._supported,
            reason_code=(
                "context_removal_supported"
                if self._supported
                else "context_removal_unsupported"
            ),
        )

    def request(
        self,
        target: str,
        *,
        idempotency_key: str,
        expected_generation: int,
    ) -> RemovalInstruction:
        self._require_supported()
        selected_target = _text(target, "target")
        idempotency_digest = _digest(_text(idempotency_key, "idempotency_key"))

        def update(raw: JsonValue | None) -> tuple[JsonValue, RemovalInstruction]:
            generation, records = _decode(raw)
            existing = next(
                (
                    record
                    for record in records
                    if record["idempotency_digest"] == idempotency_digest
                ),
                None,
            )
            if existing is not None:
                if existing["target"] != selected_target:
                    raise _conflict("context_removal_idempotency_conflict")
                return raw_or_empty(raw), _instruction(existing)
            _expect_generation(generation, expected_generation)
            generation += 1
            record: dict[str, JsonValue] = {
                "ack_digest": None,
                "acknowledged": False,
                "attempts": 1,
                "generation": generation,
                "idempotency_digest": idempotency_digest,
                "instruction_id": token_hex(16),
                "status": RemovalStatus.PENDING.value,
                "target": selected_target,
            }
            retained = [*records, record][-self._record_limit :]
            return _encode(generation, retained), _instruction(record)

        return self._state.transact_entry(
            self._scope, _KEY, update, namespace=_NAMESPACE
        )

    def retry(
        self, instruction_id: str, *, expected_generation: int
    ) -> RemovalInstruction:
        self._require_supported()
        selected_id = _text(instruction_id, "instruction_id")

        def update(raw: JsonValue | None) -> tuple[JsonValue, RemovalInstruction]:
            generation, records = _decode(raw)
            index, record = _find(records, selected_id)
            if _instruction(record).confirmed:
                return raw_or_empty(raw), _instruction(record)
            _expect_generation(generation, expected_generation)
            generation += 1
            replacement = dict(record)
            replacement["attempts"] = cast(int, record["attempts"]) + 1
            replacement["generation"] = generation
            records[index] = replacement
            return _encode(generation, records), _instruction(replacement)

        return self._state.transact_entry(
            self._scope, _KEY, update, namespace=_NAMESPACE
        )

    def acknowledge(
        self,
        instruction_id: str,
        *,
        acknowledgement_id: str,
        removed: bool,
        expected_generation: int,
    ) -> RemovalInstruction:
        self._require_supported()
        selected_id = _text(instruction_id, "instruction_id")
        ack_digest = _digest(_text(acknowledgement_id, "acknowledgement_id"))
        if not isinstance(removed, bool):
            raise TypeError("removed must be a boolean")

        def update(raw: JsonValue | None) -> tuple[JsonValue, RemovalInstruction]:
            generation, records = _decode(raw)
            index, record = _find(records, selected_id)
            if record["ack_digest"] == ack_digest:
                return raw_or_empty(raw), _instruction(record)
            if _instruction(record).confirmed:
                raise _conflict("context_removal_ack_conflict")
            _expect_generation(generation, expected_generation)
            generation += 1
            replacement = dict(record)
            replacement["ack_digest"] = ack_digest
            replacement["acknowledged"] = True
            replacement["generation"] = generation
            if removed:
                replacement["status"] = RemovalStatus.CONFIRMED.value
            records[index] = replacement
            return _encode(generation, records), _instruction(replacement)

        return self._state.transact_entry(
            self._scope, _KEY, update, namespace=_NAMESPACE
        )

    def view(self) -> dict[str, JsonValue]:
        generation, records = _decode(
            self._state.get(self._scope, _KEY, namespace=_NAMESPACE)
        )
        instructions = [_instruction(record) for record in records]
        return {
            "generation": generation,
            "negotiation": {
                "adapter": self._adapter.value,
                "feature": CONTEXT_REMOVAL,
                "reason_code": self.negotiation.reason_code,
                "supported": self._supported,
            },
            "instructions": [_public(item) for item in instructions],
            "pending": sum(not item.confirmed for item in instructions),
            "confirmed": sum(item.confirmed for item in instructions),
        }

    def _require_supported(self) -> None:
        if not self._supported:
            raise CapabilityHubError(
                code="context_removal_unsupported",
                category=ErrorCategory.INPUT,
                safe_message="The client did not negotiate context removal acknowledgements.",
            )


def _decode(raw: JsonValue | None) -> tuple[int, list[dict[str, JsonValue]]]:
    if raw is None:
        return 0, []
    try:
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise TypeError
        generation = _natural(raw.get("generation"))
        values = raw.get("records")
        if not isinstance(values, list):
            raise TypeError
        records = []
        for value in values:
            if not isinstance(value, dict):
                raise TypeError
            _instruction(value)
            if not isinstance(value.get("idempotency_digest"), str):
                raise TypeError
            ack = value.get("ack_digest")
            if ack is not None and not isinstance(ack, str):
                raise TypeError
            records.append(dict(value))
        return generation, records
    except (TypeError, ValueError) as error:
        raise CapabilityHubError(
            code="context_removal_state_invalid",
            category=ErrorCategory.INTERNAL,
            safe_message="The context removal state is invalid.",
        ) from error


def _encode(generation: int, records: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    return {
        "generation": generation,
        "records": list(cast(list[JsonValue], records)),
        "schema_version": 1,
    }


def _instruction(record: dict[str, JsonValue]) -> RemovalInstruction:
    instruction_id = record.get("instruction_id")
    target = record.get("target")
    generation = _natural(record.get("generation"))
    attempts = _positive(record.get("attempts"))
    acknowledged = record.get("acknowledged")
    status = record.get("status")
    if (
        not isinstance(instruction_id, str)
        or not isinstance(target, str)
        or not isinstance(acknowledged, bool)
        or not isinstance(status, str)
    ):
        raise TypeError
    selected = RemovalStatus(status)
    if selected is RemovalStatus.CONFIRMED and not acknowledged:
        raise TypeError
    return RemovalInstruction(
        instruction_id, target, generation, attempts, selected, acknowledged
    )


def _find(
    records: list[dict[str, JsonValue]], instruction_id: str
) -> tuple[int, dict[str, JsonValue]]:
    for index, record in enumerate(records):
        if record["instruction_id"] == instruction_id:
            return index, record
    raise CapabilityHubError(
        code="context_removal_not_found",
        category=ErrorCategory.REFERENCE,
        safe_message="The context removal instruction does not exist.",
    )


def _public(instruction: RemovalInstruction) -> dict[str, JsonValue]:
    return {
        "acknowledged": instruction.acknowledged,
        "attempts": instruction.attempts,
        "confirmed": instruction.confirmed,
        "generation": instruction.generation,
        "instruction_id": instruction.instruction_id,
        "status": instruction.status.value,
        "target": instruction.target,
    }


def _expect_generation(actual: int, expected: int) -> None:
    if actual != _natural(expected):
        raise _conflict("context_removal_generation_conflict")


def _natural(value: JsonValue | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError
    return value


def _positive(value: JsonValue | None) -> int:
    selected = _natural(value)
    if selected == 0:
        raise TypeError
    return selected


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{label} is invalid")
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(b"capabilityhub-context-removal-v1\0" + value.encode()).hexdigest()


def raw_or_empty(raw: JsonValue | None) -> JsonValue:
    return _encode(0, []) if raw is None else raw


def _conflict(code: str) -> CapabilityHubError:
    return CapabilityHubError(
        code=code,
        category=ErrorCategory.POLICY,
        safe_message="The context removal state changed or conflicts with this request.",
    )
