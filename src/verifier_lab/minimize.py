"""Failure minimization (chapter 07): a search client of the oracle, not a second implementation of the invariant.

Never claims the globally smallest counterexample: it reports the best candidate found and exactly which
reduction operators were exhausted against it (or that the budget ended first).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterator, Protocol


@dataclass(frozen=True)
class FailureSignature:
    """Stable identity of *what* failed. Operation positions are deliberately excluded."""
    rule_id: str
    rule_version: str
    claim_predicate: str
    reason_code: str
    witness_class: str

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @staticmethod
    def from_dict(d: dict) -> "FailureSignature":
        return FailureSignature(**d)


class OracleOutcome(StrEnum):
    SAME_FAILURE = "same_failure"
    NO_FAILURE = "no_failure"
    INVALID_CANDIDATE = "invalid_candidate"
    DIFFERENT_FAILURE = "different_failure"
    UNRESOLVED = "unresolved"


class Case(Protocol):
    def identity(self) -> str: ...
    def cost(self) -> tuple: ...


class FailureOracle(Protocol):
    identity: str                      # subject implementation + rule version + environment profile

    def evaluate(self, case: Case) -> OracleOutcome: ...
    def signature_of(self, case: Case) -> FailureSignature | None: ...


class Reducer(Protocol):
    name: str
    description: str

    def proposals(self, case: Case) -> Iterator[tuple[str, Case]]: ...


@dataclass
class ReductionBudget:
    max_oracle_calls: int = 3000
    max_rounds: int = 12
    deadline_seconds: float | None = None
    reproduce_runs: int = 3


@dataclass
class ReductionResult:
    status: str                         # REDUCED | BEST_SO_FAR | NOT_REPRODUCED | UNSTABLE
    original: Case
    best: Case
    signature: FailureSignature | None
    log: list[dict] = field(default_factory=list)
    oracle_calls: int = 0
    cache_hits: int = 0
    exhausted_reducers: list[str] = field(default_factory=list)
    stop_reason: str = ""
    reproduction_outcomes: list[str] = field(default_factory=list)

    def scope_statement(self, reducers: list[Reducer]) -> str:
        if self.status == "REDUCED":
            ops = "; ".join(f"{r.name} ({r.description})" for r in reducers if r.name in self.exhausted_reducers)
            return f"No remaining candidate under these operators preserves the failure signature: {ops}. " \
                   "This is minimality under those operators only, not global minimality."
        if self.status == "BEST_SO_FAR":
            return f"Best candidate found before the search stopped ({self.stop_reason}); further reduction may be possible."
        return f"No minimality claim ({self.status.lower()})."


class _Stop(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def minimize(case: Case, oracle: FailureOracle, reducers: list[Reducer],
             budget: ReductionBudget = ReductionBudget(), signature: FailureSignature | None = None) -> ReductionResult:
    started = time.monotonic()
    cache: dict[tuple[str, str], OracleOutcome] = {}
    res = ReductionResult("REDUCED", case, case, signature)

    def ask(candidate: Case) -> OracleOutcome:
        key = (oracle.identity, candidate.identity())
        if key in cache:
            res.cache_hits += 1
            return cache[key]
        if res.oracle_calls >= budget.max_oracle_calls:
            raise _Stop("oracle_call_budget_exhausted")
        if budget.deadline_seconds is not None and time.monotonic() - started > budget.deadline_seconds:
            raise _Stop("wall_clock_deadline")
        res.oracle_calls += 1
        try:
            outcome = oracle.evaluate(candidate)
        except Exception:                       # operational failure: never cached as a semantic verdict
            return OracleOutcome.UNRESOLVED
        if outcome != OracleOutcome.UNRESOLVED:
            cache[key] = outcome
        return outcome

    # 1. capture the original failure and prove it reproduces before shrinking anything
    if res.signature is None:
        res.signature = oracle.signature_of(case)
    if res.signature is not None and hasattr(oracle, "bind"):
        oracle.bind(res.signature)               # the oracle now matches *this* failure, not any failure
    for _ in range(budget.reproduce_runs):
        try:
            out = oracle.evaluate(case)
        except Exception:
            out = OracleOutcome.UNRESOLVED
        res.reproduction_outcomes.append(out.value)
    res.oracle_calls += budget.reproduce_runs
    if len(set(res.reproduction_outcomes)) > 1:
        res.status, res.stop_reason = "UNSTABLE", "original failure is not reproducible deterministically"
        return res
    if res.reproduction_outcomes[0] != OracleOutcome.SAME_FAILURE or res.signature is None:
        res.status, res.stop_reason = "NOT_REPRODUCED", f"original evaluated as {res.reproduction_outcomes[0]}"
        return res

    best = case
    try:
        for round_no in range(budget.max_rounds):
            changed_in_round = False
            exhausted: list[str] = []
            for reducer in reducers:
                while True:
                    accepted = False
                    for label, cand in reducer.proposals(best):
                        if cand.cost() >= best.cost():        # well-founded: every accepted step strictly lowers the cost
                            continue
                        outcome = ask(cand)
                        res.log.append({"step": len(res.log), "reducer": reducer.name, "operator": label,
                                        "candidate": cand.identity(), "outcome": outcome.value,
                                        "accepted": outcome == OracleOutcome.SAME_FAILURE})
                        if outcome == OracleOutcome.SAME_FAILURE:
                            best, accepted, changed_in_round = cand, True, True
                            break
                    if not accepted:
                        break
                exhausted.append(reducer.name)
            if not changed_in_round:
                res.exhausted_reducers = exhausted
                break
        else:
            res.status, res.stop_reason = "BEST_SO_FAR", "max_rounds_reached"
    except _Stop as stop:
        res.status, res.stop_reason = "BEST_SO_FAR", stop.reason
    res.best = best
    return res
