# 06 Uncertainty and temporal reasoning

## Goal

Make uncertainty an executable part of the model. Missing information, incompatible meanings, overlapping possibilities, and unsynchronized clocks must not be converted into confident support or contradiction. Implement `temporal.py`, `comparators.py`, and `rules/temporal_claims.py`. Keep applicability separate from truth: an unknown applicability condition cannot justify `NOT_APPLICABLE`.

This chapter specifies bounded comparisons, not a general causal inference engine. Temporal precedence alone does not establish causality. A claim that an operation caused an outcome requires a registered causal model or a controlled experiment with explicit assumptions.

## Comparison protocol

```python
class Comparison(StrEnum):
    EQUAL = 'equal'
    DIFFERENT = 'different'
    UNKNOWN = 'unknown'

@dataclass(frozen=True)
class ComparisonEvidence:
    result: Comparison
    comparator_id: str
    comparator_version: str
    reason_code: str
    witness: JsonValue

def compare_values(left: TypedValue, right: TypedValue,
                   spec: ComparatorSpec) -> ComparisonEvidence: ...
```

Comparators are selected by explicit semantics and version. Exact strings compare as strings; numbers with units require compatible units or a verified conversion. No fallback stringification is permitted. A missing or redacted value returns unknown. A malformed declared value is invalid evidence at validation, not a normal unknown comparison.

Use `decimal.Decimal` or rational integers for exact numeric rules. Parse decimal strings with a documented context, reject nonfinite values, and serialize values canonically under the rule's specification. A tolerance is explicit configuration included in rule identity or plan identity. Do not inherit a programming library's default relative tolerance without documenting it.

## Possibility sets and intervals

A set can mean a collection of actual members or a set of possible values. Those meanings need different predicates. For possible-value sets, disjoint nonempty sets establish incompatibility; overlap means both claims may be simultaneously possible and therefore does not prove equality. An empty possibility set is invalid unless the rule explicitly models impossibility.

Represent a measured scalar interval with lower and upper exact bounds and unit. A comparison can establish one interval entirely exceeds another, or that equality is impossible. Overlap generally leaves the point-value comparison unknown. Record whether interval endpoints are closed or open; boundary equality must not depend on an undocumented convention.

## Logical and physical time

Within one logical clock, nonnegative ticks establish ordering. Across different clock IDs, ticks are incomparable unless an explicit mapping or synchronization evidence is supplied. Never infer a shared epoch because two values happen to look like timestamps.

For uncertain physical timestamps, represent each event time as a closed interval in a declared time coordinate. Event A definitely precedes B only if A's latest possible time is strictly earlier than B's earliest possible time. If intervals overlap, precedence is unknown unless a separate happens-before edge establishes it. Closed intervals touching at one instant do not establish strict precedence.

Build a happens-before DAG from declared operation ordering and verified dependency edges. Detect cycles as invalid ordering evidence. Reachability establishes partial order. Lack of a path does not establish simultaneity or the opposite order. Use memoized reachability or bitsets only after a simple reference traversal is tested.

## Actual overlap claims

An activity interval contains start and end bounds in a common coordinate. The presence of two duration records does not establish that activities overlapped. For exact half-open intervals, positive-duration overlap exists when `max(start_a, start_b) < min(end_a, end_b)`. Equality at the boundary means no positive overlap.

For uncertain start/end bounds, compute conservative statements. Guaranteed positive overlap requires the latest possible starts to precede the earliest possible ends while each activity interval is internally valid. Guaranteed non-overlap requires one activity's latest possible end to be at or before the other's earliest possible start. Otherwise return insufficient evidence for a positive-overlap claim. Preserve the bounds used in the witness.

Do not compare durations from independent clocks without a mapping. A profile may ask a reviewer to establish the mapping, resulting in human review required. It must not pretend the user's approval physically synchronizes clocks; the reviewer must provide an explicit assumption or additional evidence.

## Combining evidence without flattening it

Store supporting and contradicting findings separately. If two validated observations conflict, the conflict is a result; do not select the one with the newer filename or higher self-declared confidence. A profile can rank evidence only using explicit rules whose assumptions are visible. Confidence is not a universal truth probability.

For conjunction, every essential proposition must be supported for the conjunction to be supported. One valid contradiction can contradict it; unresolved other branches remain listed. For a disjunction, one supported branch may establish the claim, but only under that branch's assumptions. Invalid evidence is excluded from reasoning rather than counted as false. Keep the distinction between “claim false” and “attempt to evaluate claim invalid.”

## Source interpretation boundary

PDF, Markdown, JSON, and SQL ingestion may produce candidate quantities, dates, and assertions. Extraction output is not a verified fact. A source span and extraction version must accompany each candidate. Ambiguous units, omitted time zones, uncertain OCR, and unclear statement scope remain review items. UI approval creates a recorded interpretation decision; it does not convert a document author's claim into external ground truth.

## Tests and acceptance

Test incompatible units, missing values, redacted values, exact boundaries, signed zero normalization under numeric rules, nonfinite strings, partially overlapping sets, and differently named clocks. Temporal tests cover sequential activities with positive durations, touching half-open intervals, uncertain overlap, cycles, and disconnected orderings.

Acceptance requires no `SUPPORTED` result for an overlap claim based only on two observed durations; no `NOT_APPLICABLE` result from unknown applicability; and no manufactured contradiction from an unsupported comparator. Reports must state which additional measurement or assumption would permit a stronger conclusion while preserving all current conflicting evidence.
