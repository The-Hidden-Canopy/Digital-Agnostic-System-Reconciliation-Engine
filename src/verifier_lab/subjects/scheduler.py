"""Fictional seeded scheduler with logical time and stable equal-deadline ordering.

Reference and candidate have separate scheduling transitions. The explicit future-affecting
inventory includes allocation order and random cursor, in addition to pending jobs and logical time.
"""
from copy import deepcopy
import hashlib

VERSION = 'scheduler-1'
DEFECTS = ('reverse_ties', 'drop_random_cursor')
FIELDS = {'logical_time', 'next_sequence', 'random_cursor', 'pending', 'completed', 'operation_cursor'}


def initial():
    return {'logical_time': 0, 'next_sequence': 0, 'random_cursor': 0,
            'pending': [], 'completed': [], 'operation_cursor': 0}


def validate(op):
    if not isinstance(op, dict) or op.get('op') not in ('schedule', 'advance', 'cancel'):
        raise ValueError('scheduler expects schedule, advance or cancel')
    fields = {'schedule': {'op', 'key', 'delay'}, 'advance': {'op', 'ticks'}, 'cancel': {'op', 'key'}}[op['op']]
    if set(op) != fields:
        raise ValueError('unknown/missing scheduler fields')
    if op['op'] != 'advance' and (not isinstance(op['key'], str) or not 1 <= len(op['key']) <= 64):
        raise ValueError('job key must be a bounded nonempty string')
    if op['op'] in ('schedule', 'advance'):
        n = op['delay'] if op['op'] == 'schedule' else op['ticks']
        if type(n) is not int or not 0 <= n <= 1000:
            raise ValueError('logical delay/ticks must be an integer between 0 and 1000')


def reference_step(state, op, seed):
    validate(op)
    result = deepcopy(state)
    if op['op'] == 'schedule':
        if any(j['key'] == op['key'] for j in state['pending']):
            return result, False, ['duplicate_pending_job']
        result['pending'].append({'key': op['key'], 'due': state['logical_time'] + op['delay'],
                                  'sequence': state['next_sequence']})
        result['next_sequence'] += 1
        notes = ['scheduled:' + op['key']]
    elif op['op'] == 'cancel':
        if not any(j['key'] == op['key'] for j in state['pending']):
            return result, False, ['missing_job']
        result['pending'] = [j for j in state['pending'] if j['key'] != op['key']]
        notes = ['cancelled:' + op['key']]
    else:
        result['logical_time'] += op['ticks']
        due = sorted((j for j in state['pending'] if j['due'] <= result['logical_time']),
                     key=lambda j: (j['due'], j['sequence']))
        notes = []
        for job in due:
            value = hashlib.sha256(f"{seed}|{result['random_cursor']}".encode()).hexdigest()[:8]
            result['random_cursor'] += 1
            result['completed'].append([job['key'], job['due'], value])
            notes.append(f"completed:{job['key']}={value}")
        result['pending'] = [j for j in state['pending'] if j['due'] > result['logical_time']]
    result['operation_cursor'] += 1
    return result, True, notes


class Candidate:
    def __init__(self, seed, defects=()):
        self.seed, self.defects = seed, set(defects)
        self.state = initial()

    def apply(self, op):
        validate(op)
        pending = self.state['pending']
        if op['op'] == 'schedule':
            for job in pending:
                if job['key'] == op['key']:
                    return False, ['duplicate_pending_job']
            pending.append(dict(key=op['key'], due=self.state['logical_time'] + op['delay'],
                                sequence=self.state['next_sequence']))
            self.state['next_sequence'] += 1
            notes = ['scheduled:' + op['key']]
        elif op['op'] == 'cancel':
            index = next((i for i, job in enumerate(pending) if job['key'] == op['key']), None)
            if index is None:
                return False, ['missing_job']
            pending.pop(index)
            notes = ['cancelled:' + op['key']]
        else:
            self.state['logical_time'] += op['ticks']
            sign = -1 if 'reverse_ties' in self.defects else 1
            ready = [j for j in pending if j['due'] <= self.state['logical_time']]
            ready.sort(key=lambda j: (j['due'], sign * j['sequence']))
            notes = []
            for job in ready:
                token = '|'.join((self.seed, str(self.state['random_cursor'])))
                value = hashlib.sha256(token.encode('utf-8')).hexdigest()[:8]
                self.state['random_cursor'] += 1
                self.state['completed'].append([job['key'], job['due'], value])
                notes.append('completed:' + job['key'] + '=' + value)
                pending.remove(job)
        self.state['operation_cursor'] += 1
        return True, notes

    def snapshot(self):
        return deepcopy(self.state)

    def checkpoint(self):
        snapshot = self.snapshot()
        if 'drop_random_cursor' in self.defects:
            snapshot['random_cursor'] = 0
        return snapshot

    def load(self, state):
        if not isinstance(state, dict) or set(state) != FIELDS:
            raise ValueError('scheduler checkpoint inventory mismatch')
        self.state = deepcopy(state)
