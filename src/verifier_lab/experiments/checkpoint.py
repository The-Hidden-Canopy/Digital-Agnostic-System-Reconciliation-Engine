"""Three-way checkpoint experiment (chapter 05): continuous vs resumed vs replayed, compared at every step."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..canonical import canonical_bytes, sha256_digest
from ..replay import Checkpoint, PlanError, make_checkpoint, plan_replay, restore_checkpoint
from ..subjects import reference_store as ref
from ..subjects.candidate_store import CandidateStore

PATHS = ("continuous", "checkpoint", "resumed", "replayed")


@dataclass(frozen=True)
class ExperimentCase:
    seed: str
    ops: tuple[dict, ...]
    boundary: int                         # number of top-level units executed before the checkpoint
    defects: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"seed": self.seed, "ops": list(self.ops), "boundary": self.boundary, "defects": list(self.defects)}

    @staticmethod
    def from_dict(d: dict) -> "ExperimentCase":
        return ExperimentCase(d["seed"], tuple(d["ops"]), d["boundary"], tuple(d.get("defects", ())))

    def identity(self) -> str:
        return sha256_digest(canonical_bytes(self.to_dict()))

    def cost(self) -> tuple:
        """Lexicographic: units, total members, payload bytes, canonical bytes (well-founded)."""
        members = sum(len(o["ops"]) if o.get("op") == "txn" else 1 for o in self.ops)
        payload = sum(len(m.get("key", "")) + len(m.get("value", ""))
                      for o in self.ops for m in (o["ops"] if o.get("op") == "txn" else [o]))
        return (len(self.ops), members, payload, len(self.seed), canonical_bytes(self.to_dict()))


@dataclass
class Divergence:
    path: str
    op_index: int | None
    field: str
    expected: object
    actual: object

    def to_dict(self) -> dict:
        return {"path": self.path, "op_index": self.op_index, "field": self.field,
                "expected": self.expected, "actual": self.actual}


@dataclass
class ExperimentResult:
    divergences: list[Divergence] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.divergences and not self.errors

    def first(self, path: str | None = None) -> Divergence | None:
        return next((d for d in self.divergences if path in (None, d.path)), None)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "divergences": [d.to_dict() for d in self.divergences],
                "errors": self.errors, "coverage": self.coverage}


def state_diff(expected: dict, actual: dict) -> tuple[str, object, object] | None:
    """First field-level difference in a fixed field order, entries compared key by key."""
    if set(actual) != set(expected):
        return "<fields>", sorted(expected), sorted(actual)
    ek, ak = dict((k, v) for k, v in expected["entries"]), dict((k, v) for k, v in actual["entries"])
    for k in sorted(set(ek) | set(ak)):
        if ek.get(k) != ak.get(k):
            return f"entries[{k}]", ek.get(k), ak.get(k)
    for f in ("next_identifier", "logical_tick", "random_cursor", "operation_cursor"):
        if expected[f] != actual[f]:
            return f, expected[f], actual[f]
    return None


def field_class(field_path: str) -> str:
    """Stable class for failure signatures: operation positions and entry keys are not part of the identity."""
    return "entries" if field_path.startswith("entries[") else field_path


def _reference_run(case: ExperimentCase):
    states = [ref.StoreState().to_dict()]
    trans = []
    state = ref.StoreState()
    for op in case.ops:
        t = ref.apply_operation(state, op, case.seed)
        trans.append(t)
        state = t.state
        states.append(state.to_dict())
    return states, trans


def _compare_step(result, path, idx, trans, accepted, notes, cand_state, ref_state_dict):
    if accepted != trans.accepted:
        result.divergences.append(Divergence(path, idx, "accepted", trans.accepted, accepted))
        return False
    if accepted and tuple(notes) != trans.observations:
        result.divergences.append(Divergence(path, idx, "observations", list(trans.observations), list(notes)))
        return False
    d = state_diff(ref_state_dict, cand_state)
    if d:
        result.divergences.append(Divergence(path, idx, d[0], d[1], d[2]))
        return False
    return True


def run_experiment(case: ExperimentCase) -> ExperimentResult:
    result = ExperimentResult()
    n = len(case.ops)
    result.coverage = {"mode": "single_case", "operations": n, "boundary": case.boundary,
                       "paths": list(PATHS), "steps_compared_per_path": {}}
    if not 0 <= case.boundary <= n:
        result.errors.append({"code": "boundary_out_of_range"})
        return result
    try:
        states, trans = _reference_run(case)
    except ref.OperationError as exc:
        result.errors.append({"code": "invalid_operation", "message": str(exc)})
        return result
    defects = frozenset(case.defects)
    # path 1: continuous
    cand = CandidateStore(case.seed, defects)
    steps = 0
    for i, op in enumerate(case.ops):
        ok, notes = cand.apply(op)
        steps += 1
        if not _compare_step(result, "continuous", i, trans[i], ok, notes, cand.snapshot(), states[i + 1]):
            break
    result.coverage["steps_compared_per_path"]["continuous"] = steps
    # path 2: run to the boundary, serialize the candidate's own state, restore into a fresh instance, continue
    a = CandidateStore(case.seed, defects)
    for op in case.ops[:case.boundary]:
        a.apply(op)
    d0 = state_diff(states[case.boundary], a.snapshot())
    cp_bytes = None
    if d0:
        result.divergences.append(Divergence("checkpoint", case.boundary, d0[0], d0[1], d0[2]))
    try:
        cp_bytes = make_checkpoint(a.snapshot(), case.seed, f"gen-{case.boundary}", case.boundary).to_bytes()
        restored = restore_checkpoint(cp_bytes)
    except (PlanError, KeyError, ValueError) as exc:
        result.errors.append({"code": "checkpoint_unusable", "message": str(exc)})
        return result
    b = CandidateStore(case.seed, defects)
    b.load(restored.state)
    steps = 0
    for i in range(case.boundary, n):
        ok, notes = b.apply(case.ops[i])
        steps += 1
        if not _compare_step(result, "resumed", i, trans[i], ok, notes, b.snapshot(), states[i + 1]):
            break
    result.coverage["steps_compared_per_path"]["resumed"] = steps
    # path 3: restore the REFERENCE checkpoint and execute a separately constructed replay plan
    ref_cp = make_checkpoint(states[case.boundary], case.seed, f"gen-{case.boundary}", case.boundary)
    suffix = [dict(op, id=f"op-{i}", position=i) for i, op in enumerate(case.ops) if i >= case.boundary]
    try:
        plan = plan_replay(ref_cp, suffix, expected_generation=f"gen-{case.boundary}")
    except PlanError as exc:
        result.errors.append({"code": "replay_plan_refused", "message": str(exc)})
        return result
    c = CandidateStore(case.seed, defects)
    c.load(restore_checkpoint(ref_cp.to_bytes()).state)
    steps = 0
    for k, (op, ok, notes) in enumerate(c.replay(list(plan.operations))):
        i = case.boundary + k
        steps += 1
        if not _compare_step(result, "replayed", i, trans[i], ok, notes, c.snapshot(), states[i + 1]):
            break
    result.coverage["steps_compared_per_path"]["replayed"] = steps
    result.coverage["replay_plan"] = plan.identity()
    return result
