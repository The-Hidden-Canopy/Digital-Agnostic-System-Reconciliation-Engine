"""Three-way experiments and reduced counterexamples for the pipeline and scheduler.

Built-ins only: serialized cases never select a module, command, path or executable. A fresh
verifier reproduces the witness using the independently written reference and candidate.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from ..canonical import canonical_bytes, sha256_digest, strict_loads
from ..minimize import FailureSignature, OracleOutcome, ReductionBudget, minimize
from ..subjects import pipeline, scheduler
from .checkpoint import Divergence, ExperimentResult

SUBJECTS = {'pipeline': pipeline, 'scheduler': scheduler}
SCHEMA = 'verifier-lab/laboratory-counterexample/1'
RUNNER_VERSION = 'laboratory-three-way-1'


@dataclass(frozen=True)
class LabCase:
    system: str
    seed: str
    ops: tuple[dict, ...]
    boundary: int
    defects: tuple[str, ...] = ()

    def to_dict(self):
        return dict(system=self.system, seed=self.seed, ops=list(self.ops), boundary=self.boundary,
                    defects=list(self.defects))

    @staticmethod
    def from_dict(body):
        if not isinstance(body, dict) or set(body) != {'system', 'seed', 'ops', 'boundary', 'defects'}:
            raise ValueError('unexpected laboratory case fields')
        if not isinstance(body['ops'], list) or not isinstance(body['defects'], list):
            raise ValueError('operations and defects must be arrays')
        case = LabCase(body['system'], body['seed'], tuple(body['ops']), body['boundary'], tuple(body['defects']))
        validate_case(case)
        return case

    def identity(self):
        return sha256_digest(canonical_bytes(self.to_dict()))

    def cost(self):
        return len(self.ops), len(canonical_bytes(self.to_dict())), canonical_bytes(self.to_dict())


def validate_case(case):
    if not isinstance(case.system, str) or case.system not in SUBJECTS:
        raise ValueError('unknown synthetic system')
    if not isinstance(case.seed, str) or not 1 <= len(case.seed) <= 100:
        raise ValueError('seed must contain 1–100 characters')
    if len(case.ops) > 400 or type(case.boundary) is not int or not 0 <= case.boundary <= len(case.ops):
        raise ValueError('bounded operations and a valid checkpoint boundary are required')
    module = SUBJECTS[case.system]
    if any(not isinstance(d, str) or d not in module.DEFECTS for d in case.defects):
        raise ValueError('unknown injected defect')
    for op in case.ops:
        module.validate(op)


def generated_case(system, seed='seed-1', count=60, boundary=None, defects=()):
    if type(count) is not int or not 0 <= count <= 400:
        raise ValueError('operation count must be an integer between 0 and 400')
    if not isinstance(system, str) or system not in SUBJECTS or not isinstance(seed, str) or not 1 <= len(seed) <= 100:
        raise ValueError('unknown system or invalid seed')
    if not isinstance(defects, (tuple, list)):
        raise ValueError('defects must be an array')
    ops = []
    tag = sha256_digest(seed.encode())[-8:]
    for i in range((count + 3) // 4):
        if system == 'pipeline':
            ops.extend(({'op': 'put', 'key': 'doc', 'value': f'first-{tag}-{i}'},
                        {'op': 'transform', 'key': 'doc', 'mode': 'upper', 'output': 'report'},
                        {'op': 'put', 'key': 'doc', 'value': f'changed-{tag}-{i}'},
                        {'op': 'transform', 'key': 'doc', 'mode': 'upper', 'output': 'report'}))
        else:
            ops.extend(({'op': 'schedule', 'key': f'left-{i}', 'delay': 1},
                        {'op': 'schedule', 'key': f'right-{i}', 'delay': 1},
                        {'op': 'advance', 'ticks': 1}, {'op': 'advance', 'ticks': 0}))
    case = LabCase(system, seed, tuple(ops[:count]), count // 2 if boundary is None else boundary, tuple(defects))
    validate_case(case)
    return case


def _difference(expected, actual, path='state'):
    if type(expected) is not type(actual):
        return path, expected, actual
    if isinstance(expected, dict):
        if set(expected) != set(actual):
            return path + '.fields', sorted(expected), sorted(actual)
        for key in sorted(expected):
            found = _difference(expected[key], actual[key], path + '.' + key)
            if found:
                return found
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            return path + '.length', len(expected), len(actual)
        for index, (left, right) in enumerate(zip(expected, actual)):
            found = _difference(left, right, f'{path}[{index}]')
            if found:
                return found
    elif expected != actual:
        return path, expected, actual
    return None


def run_case(case):
    result = ExperimentResult()
    try:
        validate_case(case)
    except (TypeError, ValueError) as exc:
        result.errors.append({'code': 'invalid_laboratory_case', 'message': str(exc)})
        return result
    module = SUBJECTS[case.system]
    result.coverage = {'mode': 'single_case', 'system': case.system, 'semantics_version': module.VERSION,
                       'runner_version': RUNNER_VERSION, 'operations': len(case.ops), 'boundary': case.boundary,
                       'steps_compared_per_path': {},
                       'scope': 'Only these operations, seed, checkpoint and built-in subject were exercised.'}
    states, transitions = [module.initial()], []
    for op in case.ops:
        state, accepted, notes = module.reference_step(states[-1], op, case.seed)
        states.append(state)
        transitions.append((accepted, notes))

    def compare(path, index, expected, actual, field='state'):
        diff = _difference(expected, actual, field)
        if diff:
            result.divergences.append(Divergence(path, index, *diff))
        return diff is None

    def run_suffix(candidate, start, path):
        compared = 0
        for index in range(start, len(case.ops)):
            accepted, notes = candidate.apply(case.ops[index])
            compared += 1
            if not compare(path, index, transitions[index][0], accepted, 'accepted'):
                break
            if not compare(path, index, states[index + 1], candidate.snapshot()):
                break
            if not compare(path, index, transitions[index][1], notes, 'observations'):
                break
        result.coverage['steps_compared_per_path'][path] = compared

    run_suffix(module.Candidate(case.seed, case.defects), 0, 'continuous')
    prefix = module.Candidate(case.seed, case.defects)
    for op in case.ops[:case.boundary]:
        prefix.apply(op)
    # Actual byte serialization is round-tripped; there is no in-memory object handoff.
    checkpoint = strict_loads(canonical_bytes(prefix.checkpoint()))
    compare('checkpoint', case.boundary, states[case.boundary], checkpoint)
    resumed = module.Candidate(case.seed, case.defects)
    resumed.load(checkpoint)
    run_suffix(resumed, case.boundary, 'resumed')
    replayed = module.Candidate(case.seed, case.defects)
    replayed.load(strict_loads(canonical_bytes(states[case.boundary])))
    run_suffix(replayed, case.boundary, 'replayed')
    return result


class DeleteLabUnits:
    name = 'delete_laboratory_units'
    description = 'contiguous chunk and single-operation deletion, preserving the checkpoint boundary'

    def proposals(self, case):
        n, granularity = len(case.ops), 2
        while n:
            size = max(1, n // granularity)
            for start in range(0, n, size):
                stop = min(n, start + size)
                boundary = case.boundary - max(0, min(stop, case.boundary) - start)
                yield f'delete[{start}:{stop}]', LabCase(case.system, case.seed,
                    case.ops[:start] + case.ops[stop:], boundary, case.defects)
            if size == 1:
                break
            granularity = min(n, granularity * 2)


class LabOracle:
    def __init__(self, system):
        self.system, self.signature = system, None
        self.identity = f'{RUNNER_VERSION}+{SUBJECTS[system].VERSION}'

    def bind(self, signature):
        self.signature = signature

    def signature_of(self, case):
        result = run_case(case)
        if result.errors or not result.first():
            return None
        first = result.first()
        root_field = first.field.split('.')[1].split('[')[0] if '.' in first.field else first.field
        return FailureSignature('experiment.' + case.system, SUBJECTS[case.system].VERSION,
                                'state_equivalence', 'state_divergence', first.path + ':' + root_field)

    def evaluate(self, case):
        if case.system != self.system:
            return OracleOutcome.INVALID_CANDIDATE
        result = run_case(case)
        if result.errors:
            return OracleOutcome.INVALID_CANDIDATE
        if not result.first():
            return OracleOutcome.NO_FAILURE
        return OracleOutcome.SAME_FAILURE if self.signature_of(case) == self.signature else OracleOutcome.DIFFERENT_FAILURE


def reduce_case(case, max_calls=3000):
    validate_case(case)
    if type(max_calls) is not int or not 3 <= max_calls <= 3000:
        raise ValueError('oracle budget must be an integer between 3 and 3000')
    reducers = [DeleteLabUnits()]
    result = minimize(case, LabOracle(case.system), reducers, ReductionBudget(max_oracle_calls=max_calls))
    if result.signature is None or result.status in ('NOT_REPRODUCED', 'UNSTABLE'):
        return {'status': result.status, 'reproduction_outcomes': result.reproduction_outcomes}, None
    final = run_case(result.best)
    body = {'schema': SCHEMA, 'runner_version': RUNNER_VERSION, 'semantics_version': SUBJECTS[case.system].VERSION,
            'signature': result.signature.to_dict(), 'original': {'identity': case.identity(), 'case': case.to_dict()},
            'reduced': {'identity': result.best.identity(), 'case': result.best.to_dict()},
            'witness': final.first().to_dict(),
            'reduction': {'status': result.status, 'oracle_calls': result.oracle_calls, 'log': result.log,
                          'scope': result.scope_statement(reducers)}}
    package = {**body, 'id': sha256_digest(canonical_bytes(body))}
    return {'status': result.status, 'original_units': len(case.ops), 'reduced_units': len(result.best.ops),
            'oracle_calls': result.oracle_calls, 'signature': result.signature.to_dict(),
            'scope': result.scope_statement(reducers), 'reduced_case': result.best.to_dict()}, package


def verify_counterexample(package):
    checks = []

    def check(name, condition):
        checks.append({'name': name, 'ok': bool(condition)})
    try:
        check('schema', package.get('schema') == SCHEMA)
        check('runner_version', package['runner_version'] == RUNNER_VERSION)
        body = {k: v for k, v in package.items() if k != 'id'}
        check('package_identity', sha256_digest(canonical_bytes(body)) == package['id'])
        original, reduced = (LabCase.from_dict(package[k]['case']) for k in ('original', 'reduced'))
        check('same_subject_seed_and_defects', original.system == reduced.system and original.seed == reduced.seed
              and original.defects == reduced.defects)
        check('semantics_version', package['semantics_version'] == SUBJECTS[reduced.system].VERSION)
        for name, case in (('original', original), ('reduced', reduced)):
            check(name + '_identity', case.identity() == package[name]['identity'])
            signature = LabOracle(case.system).signature_of(case)
            check(name + '_failure_signature', signature is not None and signature.to_dict() == package['signature'])
        result = run_case(reduced)
        check('witness_reproduces', result.first() is not None and result.first().to_dict() == package['witness'])
        check('not_larger', reduced.cost() <= original.cost())
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        checks.append({'name': 'malformed_package', 'ok': False, 'detail': str(exc)[:200]})
    return {'status': 'VERIFIED' if checks and all(c['ok'] for c in checks) else 'REJECTED', 'checks': checks,
            'note': 'Fresh reproduction uses this runner and independent reference transitions; it is not an independent-language checker.'}
