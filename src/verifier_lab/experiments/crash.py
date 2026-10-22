"""Virtual durable store and crash-boundary enumeration (chapter 05).

Model assumptions (stated, not proven for any real filesystem): `write` is volatile; `flush(paths)` makes
exactly those paths durable; the CURRENT marker is replaced atomically (never torn); a crash drops all
volatile data. No sleeps. Passing this model says nothing about power-loss safety on a real device.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..canonical import canonical_bytes, sha256_digest, strict_loads

WRITER_DEFECTS = ("marker_before_flush",)
RECOVERY_DEFECTS = ("join_members", "silent_fallback")
MARKER = "CURRENT"


class VirtualDisk:
    def __init__(self, durable: dict[str, bytes] | None = None):
        self.durable = dict(durable or {})
        self.volatile: dict[str, bytes] = {}

    def write(self, path: str, data: bytes) -> None:
        self.volatile[path] = data

    def flush(self, paths: list[str]) -> None:
        for p in paths:
            if p in self.volatile:
                self.durable[p] = self.volatile.pop(p)

    def crash(self) -> dict[str, bytes]:
        self.volatile.clear()
        return dict(self.durable)


def marker_bytes(gen: str, members: dict[str, bytes]) -> bytes:
    return canonical_bytes({"generation": gen, "manifest": {n: sha256_digest(b) for n, b in sorted(members.items())}})


def writer_steps(gen: str, members: dict[str, bytes], defect: str | None = None) -> list[tuple]:
    paths = [f"{gen}/{n}" for n in sorted(members)]
    steps: list[tuple] = [("write", p, members[p.split("/", 1)[1]]) for p in paths]
    marker = marker_bytes(gen, members)
    if defect == "marker_before_flush":
        steps += [("write", MARKER, marker), ("flush", [MARKER])] + [("flush", [p]) for p in paths]
    else:
        steps += [("flush", [p]) for p in paths] + [("write", MARKER, marker), ("flush", [MARKER])]
    return steps


def apply_step(disk: VirtualDisk, step: tuple) -> None:
    if step[0] == "write":
        disk.write(step[1], step[2])
    else:
        disk.flush(step[1])


def _generations(durable: dict[str, bytes]) -> list[str]:
    return sorted({p.split("/", 1)[0] for p in durable if "/" in p})


def _complete(durable, gen, manifest) -> bool:
    return all(f"{gen}/{n}" in durable and sha256_digest(durable[f"{gen}/{n}"]) == d for n, d in manifest.items())


def recover_reference(durable: dict[str, bytes], history: dict[str, dict[str, str]]) -> dict:
    """Specification. `history` maps committed generation -> manifest (what the writer ever committed)."""
    if MARKER not in durable:
        return {"kind": "EMPTY"}
    marker = strict_loads(durable[MARKER])
    gen, manifest = marker["generation"], marker["manifest"]
    if _complete(durable, gen, manifest):
        return {"kind": "ADOPT", "generation": gen, "members": dict(sorted(manifest.items()))}
    return {"kind": "FAIL", "reason": "marker_points_to_incomplete_generation"}


def recover_candidate(durable: dict[str, bytes], defects: frozenset[str]) -> dict:
    """Candidate recovery under test; the same logic as the reference only when no defect is enabled."""
    if "join_members" in defects:
        view: dict[str, str] = {}
        for gen in _generations(durable):           # newest generation wins per member name: can mix generations
            for p, data in durable.items():
                if p.startswith(gen + "/"):
                    view[p.split("/", 1)[1]] = sha256_digest(data)
        marker = strict_loads(durable[MARKER])["generation"] if MARKER in durable else None
        return {"kind": "ADOPT", "generation": marker, "members": dict(sorted(view.items()))} if view else {"kind": "EMPTY"}
    if MARKER not in durable:
        return {"kind": "EMPTY"}
    marker = strict_loads(durable[MARKER])
    gen, manifest = marker["generation"], marker["manifest"]
    if _complete(durable, gen, manifest):
        return {"kind": "ADOPT", "generation": gen, "members": dict(sorted(manifest.items()))}
    if "silent_fallback" in defects:
        older = [g for g in _generations(durable) if g != gen]
        for g in reversed(older):
            members = {p.split("/", 1)[1]: sha256_digest(d) for p, d in durable.items() if p.startswith(g + "/")}
            if members:
                return {"kind": "ADOPT", "generation": g, "members": dict(sorted(members.items()))}
    return {"kind": "FAIL", "reason": "marker_points_to_incomplete_generation"}


@dataclass
class CrashResult:
    violations: list[dict] = field(default_factory=list)
    coverage: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {"passed": self.passed, "violations": self.violations, "coverage": self.coverage}


def crash_experiment(writer_defect: str | None = None, recovery_defects: tuple[str, ...] = (),
                     max_positions: int | None = None) -> CrashResult:
    gen1 = {"a": b"a-v1", "b": b"b-v1"}
    gen2 = {"a": b"a-v2", "b": b"b-v2"}
    base = VirtualDisk()
    for s in writer_steps("gen-1", gen1):                # generation 1 is fully committed before the experiment
        apply_step(base, s)
    base_durable = base.crash()
    steps = writer_steps("gen-2", gen2, writer_defect)
    positions = list(range(len(steps) + 1))              # crash before step i; the last crashes after full acknowledgement
    explored = positions if max_positions is None else positions[:max_positions]
    res = CrashResult()
    complete = {g: {n: sha256_digest(b) for n, b in sorted(m.items())} for g, m in (("gen-1", gen1), ("gen-2", gen2))}
    for k in explored:
        disk = VirtualDisk(base_durable)
        for s in steps[:k]:
            apply_step(disk, s)
        durable = disk.crash()
        expected = recover_reference(durable, {})
        if expected["kind"] == "FAIL":
            # The model promises atomic publication: after any crash an intact generation must be recoverable.
            res.violations.append({"position": k, "class": "writer_published_incomplete_generation",
                                   "expected": {"kind": "ADOPT"}, "actual": expected})
        actual = recover_candidate(durable, frozenset(recovery_defects))
        if actual == expected:
            continue
        if actual.get("kind") == "ADOPT" and actual["members"] not in complete.values():
            cls = "mixed_generation"
        elif actual.get("kind") == "ADOPT" and expected.get("kind") == "FAIL":
            cls = "silent_fallback"
        else:
            cls = "wrong_outcome"
        res.violations.append({"position": k, "class": cls, "expected": expected, "actual": actual})
    res.coverage = {"mode": "exhaustive" if len(explored) == len(positions) else "truncated_by_budget",
                    "positions_total": len(positions), "explored": explored,
                    "not_explored": positions[len(explored):], "steps": len(steps), "model": "virtual-durable-store-1"}
    return res
