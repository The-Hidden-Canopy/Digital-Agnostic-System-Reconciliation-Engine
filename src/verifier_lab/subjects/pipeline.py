"""Fictional transformation pipeline: independently written pure reference and mutable candidate.

Wire validation and the state inventory are shared; transition implementations are separate.
A cache entry is keyed by input identity, transform and source revision, never just a document name.
"""
from copy import deepcopy

VERSION = 'pipeline-1'
DEFECTS = ('stale_cache', 'drop_versions')
FIELDS = {'documents', 'outputs', 'cache', 'operation_cursor'}


def initial():
    return {'documents': {}, 'outputs': {}, 'cache': {}, 'operation_cursor': 0}


def validate(op):
    if not isinstance(op, dict) or op.get('op') not in ('put', 'transform'):
        raise ValueError('pipeline expects put or transform')
    fields = {'op', 'key', 'value'} if op['op'] == 'put' else {'op', 'key', 'mode', 'output'}
    if set(op) != fields or not isinstance(op.get('key'), str) or not 1 <= len(op['key']) <= 64:
        raise ValueError('invalid pipeline operation fields/key')
    if op['op'] == 'put':
        if not isinstance(op['value'], str) or len(op['value']) > 1000:
            raise ValueError('put value must be a string of at most 1000 characters')
    elif op['mode'] not in ('upper', 'reverse') or not isinstance(op['output'], str) or not 1 <= len(op['output']) <= 64:
        raise ValueError('transform needs a mode and output name')


def reference_step(state, op, seed):
    validate(op)
    if op['op'] == 'transform' and op['key'] not in state['documents']:
        return deepcopy(state), False, ['missing_document']
    result = deepcopy(state)
    if op['op'] == 'put':
        old = state['documents'].get(op['key'])
        result['documents'][op['key']] = [op['value'], 1 if old is None else old[1] + 1]
        notes = ['stored:' + op['key']]
    else:
        text, revision = state['documents'][op['key']]
        transformed = text.upper() if op['mode'] == 'upper' else ''.join(reversed(text))
        cache_key = f"{op['key']}|{op['mode']}|{revision}"
        result['cache'][cache_key] = transformed
        result['outputs'][op['output']] = [transformed, revision]
        notes = [f"output:{op['output']}={transformed}"]
    result['operation_cursor'] += 1
    return result, True, notes


class Candidate:
    def __init__(self, seed, defects=()):
        self.seed, self.defects = seed, set(defects)
        self.state = initial()

    def apply(self, op):
        validate(op)
        docs = self.state['documents']
        key = op['key']
        if op['op'] == 'put':
            version = docs[key][1] + 1 if key in docs else 1
            docs[key] = [op['value'], version]
            notes = ['stored:' + key]
        else:
            if key not in docs:
                return False, ['missing_document']
            value, version = docs[key]
            lookup_version = 1 if 'stale_cache' in self.defects else version
            cache_key = '|'.join((key, op['mode'], str(lookup_version)))
            if cache_key in self.state['cache']:
                transformed = self.state['cache'][cache_key]
            else:
                transformed = value.upper() if op['mode'] == 'upper' else value[::-1]
            # Store under the authoritative key even when the injected lookup is wrong.
            self.state['cache'][f"{key}|{op['mode']}|{version}"] = transformed
            self.state['outputs'][op['output']] = [transformed, version]
            notes = ['output:' + op['output'] + '=' + transformed]
        self.state['operation_cursor'] += 1
        return True, notes

    def snapshot(self):
        return deepcopy(self.state)

    def checkpoint(self):
        snapshot = self.snapshot()
        if 'drop_versions' in self.defects:
            for item in snapshot['documents'].values():
                item[1] = 0
        return snapshot

    def load(self, state):
        if not isinstance(state, dict) or set(state) != FIELDS:
            raise ValueError('pipeline checkpoint inventory mismatch')
        self.state = deepcopy(state)
