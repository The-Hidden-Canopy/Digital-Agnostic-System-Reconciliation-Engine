"""Checkpoints and pure replay planning (chapter 05). Planning never executes anything and never repairs gaps."""
from __future__ import annotations

from dataclasses import dataclass

from .canonical import canonical_bytes, record_id, strict_loads
from .subjects.reference_store import FIELDS, SEMANTICS_VERSION, StoreState, validate_operation, OperationError

CHECKPOINT_SCHEMA = "verifier-lab/checkpoint/1"


class PlanError(ValueError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


@dataclass(frozen=True)
class Checkpoint:
    id: str
    generation: str
    seed: str
    history_position: int
    semantics_version: str
    state: dict

    def to_bytes(self) -> bytes:
        return canonical_bytes(self.body() | {"id": self.id})

    def body(self) -> dict:
        return {"schema": CHECKPOINT_SCHEMA, "generation": self.generation, "seed": self.seed,
                "history_position": self.history_position, "semantics_version": self.semantics_version,
                "state": self.state}


def make_checkpoint(state_dict: dict, seed: str, generation: str, history_position: int,
                    semantics_version: str = SEMANTICS_VERSION) -> Checkpoint:
    """Build from decoded state; the state must carry exactly the declared field inventory."""
    if set(state_dict) != set(FIELDS):
        raise PlanError("incomplete_state", f"fields {sorted(state_dict)} != {sorted(FIELDS)}")
    cp = Checkpoint("", generation, seed, history_position, semantics_version, state_dict)
    return Checkpoint(record_id("checkpoint", cp.body()), generation, seed, history_position, semantics_version, state_dict)


def restore_checkpoint(data: bytes) -> Checkpoint:
    raw = strict_loads(data)
    if not isinstance(raw, dict) or raw.get("schema") != CHECKPOINT_SCHEMA:
        raise PlanError("bad_checkpoint", "unknown schema")
    body = {k: v for k, v in raw.items() if k != "id"}
    if record_id("checkpoint", body) != raw.get("id"):
        raise PlanError("checkpoint_identity_mismatch")
    if raw["semantics_version"] != SEMANTICS_VERSION:
        raise PlanError("incompatible_version", f"{raw['semantics_version']} != {SEMANTICS_VERSION}")
    StoreState.from_dict(raw["state"])      # validates the field inventory
    return Checkpoint(raw["id"], raw["generation"], raw["seed"], raw["history_position"],
                      raw["semantics_version"], raw["state"])


@dataclass(frozen=True)
class ReplayPlan:
    base_checkpoint_id: str
    semantics_version: str
    expected_generation: str
    first_position: int
    operations: tuple[dict, ...]       # a transaction stays ONE unit; flattening would change semantics

    def identity(self) -> str:
        return record_id("replayplan", {"base": self.base_checkpoint_id, "semantics": self.semantics_version,
                                        "generation": self.expected_generation, "first": self.first_position,
                                        "ops": list(self.operations)})


def plan_replay(base: Checkpoint, operations: list[dict], expected_generation: str | None = None,
                semantics_version: str = SEMANTICS_VERSION) -> ReplayPlan:
    """Pure. Operations carry absolute `position` and unique `id`; gaps, duplicates and wrong generations refuse."""
    if base.semantics_version != semantics_version:
        raise PlanError("incompatible_version", f"{base.semantics_version} vs {semantics_version}")
    if expected_generation is not None and expected_generation != base.generation:
        raise PlanError("wrong_generation", f"{base.generation} != {expected_generation}")
    seen_ids = set()
    for i, op in enumerate(operations):
        try:
            validate_operation(op)
        except OperationError as exc:
            raise PlanError("invalid_operation", str(exc)) from exc
        if op.get("id") in seen_ids or not isinstance(op.get("id"), str):
            raise PlanError("duplicate_or_missing_operation_id", str(op.get("id")))
        seen_ids.add(op["id"])
        if op.get("position") != base.history_position + i:
            raise PlanError("operation_gap", f"expected position {base.history_position + i}, got {op.get('position')}")
    return ReplayPlan(base.id, semantics_version, base.generation, base.history_position, tuple(operations))
