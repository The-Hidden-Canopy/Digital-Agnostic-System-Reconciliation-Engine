# 05 Replay and checkpoint experiments

## Goal

Build an experiment harness that compares continuous execution, resumed execution, and replayed execution under explicit state and timing assumptions. The reference subject is a fictional transactional key-value system. It has a map, monotonically allocated identifiers, a logical tick, a deterministic random-stream cursor, and an operation cursor. These fields are newly specified public test state, not a serialization of another system.

Create `replay.py`, `subjects/reference_store.py`, and `experiments/checkpoint.py`. The reference store should be deliberately simple: immutable state in, immutable state out. Candidate subjects may use mutable or optimized implementations, but the checker obtains decoded state through a separate protocol and compares it against the reference semantics.

## Transition contract

```python
@dataclass(frozen=True)
class StoreState:
    entries: tuple[tuple[str, str], ...]
    next_identifier: int
    logical_tick: int
    random_cursor: int
    operation_cursor: int

def apply_operation(state: StoreState, operation: Operation) -> Transition: ...
def make_checkpoint(state: StoreState, generation: str) -> Checkpoint: ...
def restore_checkpoint(checkpoint: Checkpoint) -> StoreState: ...
def plan_replay(base: Checkpoint, operations: tuple[Operation, ...]) -> ReplayPlan: ...
```

A transition returns the next state, emitted observations, and acceptance status. Rejected operations leave authoritative state unchanged, including counters unless the public semantics explicitly define otherwise. A transaction applies a sequence against temporary state and commits it only if every member succeeds. Record its boundary as one replay unit; flattening it into individual committed operations changes semantics.

## Three-way experiment

Choose a seed, initial state, operation list, and checkpoint boundary. Run the reference and candidate continuously to the final boundary. In separate fresh instances, run to the checkpoint, serialize, restore, and continue with the suffix. In a third path, restore the same checkpoint and execute a separately constructed replay plan for that suffix. Compare decoded state at every post-boundary step, not just at the end.

Record the first divergent operation index and field path. Final-state equality can conceal a temporary invariant violation that later operations repair. Conversely, diagnostic timestamps and memory addresses must be excluded from authoritative state and should not create false divergence.

The replay plan binds initial checkpoint identity, semantics version, operation identities, and transaction boundaries. Planning is pure. An incompatible checkpoint version, an operation gap, or the wrong starting generation prevents execution. Do not “repair” those conditions by dropping operations until replay succeeds.

## Randomness and allocation continuity

Restore the random generator's complete explicit state, not merely its initial seed. The same seed at a later cursor is a different future. Restore allocation counters even if currently visible entries are identical. A deleted identifier may still affect which identifier comes next. The reference field inventory must include these future-affecting fields.

Synthetic defects include omitting the random cursor, resetting the next-identifier counter, advancing the operation cursor twice, sorting noncommutative operations, and flattening atomic transactions. Each defect gets an independently written witness fixture. Candidate and checker must not share a transition implementation or a generated field list that could omit the same state field.

## Crash model

Use a virtual durable store with operations such as stage, flush, publish-marker, and restart. Define what each operation guarantees within this simulation. Inject a crash immediately before and after each modeled durability boundary. Recovery may choose the old complete generation, the new complete generation, or explicit failure. It must not declare success after joining members from different generations.

Model an acknowledged durable write separately from a staged write that may disappear. Do not use arbitrary sleeps to simulate durability. A physical filesystem experiment is a later backend with its own documented assumptions; passing the virtual model does not prove power-loss safety on a particular operating system or device.

Use immutable generation directories and a small current-generation marker in the reference scenario. Verify member bytes before adopting a generation. An orphan staged generation is not a committed result. A newer committed marker with missing members is a recovery problem, not permission to silently present an older result as the latest one.

## Deterministic experiment budget

The experiment request specifies maximum operations, checkpoint positions, and crash positions. Enumerate positions in stable order. Return a coverage record listing explored boundaries and any not explored because of configured limits. A timeout produces an execution error and an incomplete experiment record; it does not support “all crash positions recover.”

For expensive candidates, select checkpoints deterministically by a recorded generator. That supports a sampled claim only. Distinguish exhaustive finite enumeration from sampling in both the finding and the UI. Record the candidate executable identity and environment profile before running so later reproductions can detect version changes.

## Acceptance and tests

Unit tests cover empty histories, checkpoint at start and end, rejected operations, nested-transaction rejection, repeated replay requests, wrong seeds, missing suffix entries, duplicate operation IDs, and incompatible versions. Define whether replay is idempotent through request deduplication or rejects duplicates; never assume both.

Experiment tests require equal full state for the correct subject across all three paths. Every intentional defect must yield the expected field-level witness. Crash tests enumerate all boundaries in a short transaction and reject every successful mixed-generation recovery. A second process must reproduce a saved witness without access to the original runner's memory.

Acceptance is a bounded statement: the specified subject version satisfied or violated the specified invariants over the recorded operation sequences and crash model. The laboratory must preserve that scope rather than advertising universal determinism from a few repeated runs.
