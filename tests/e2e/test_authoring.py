"""Human interpretations preserve source bytes, require approval, and never become evidence facts."""
import copy
import json
import zipfile
from pathlib import Path
from urllib.parse import quote

from tests.helpers import WorkspaceCase
from tests.e2e.test_cli import cli
from tests.e2e.test_server import ServerCase
from verifier_lab.checker import verify_package
from verifier_lab.checker import Report, _check_provenance, _load_records
from verifier_lab.canonical import canonical_bytes
from verifier_lab.service import ServiceError

TEXT = 'Résumé: the maximum upload should be one hundred kilobytes.\n'
INTERPRETATION = {'predicate': 'fact.equals', 'subject': 'limits.max_upload_kb',
                  'arguments': {'expected': {'kind': 'integer', 'value': '100'}}}


class Authoring(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.source = self.imp(TEXT.encode(), 'prose.md')
        self.attempt = self.source['attempt']['id']
        self.start = len('Résumé: '.encode())
        self.end = len(TEXT.rstrip().encode())

    def author(self, **kw):
        args = dict(source_id=self.source['id'], attempt_id=self.attempt, byte_start=self.start,
                    byte_end=self.end, interpretation=INTERPRETATION,
                    reason='Interpret the written quantity as the exact upload limit.')
        args.update(kw)
        return self.svc.author_candidate(**args)

    def test_review_run_export_and_provenance(self):
        before = self.svc.get_source(self.source['id'], detail=True)
        candidate = self.author()
        self.assertEqual(candidate['status'], 'PENDING')
        self.assertTrue(candidate['authored'])
        self.assertEqual(candidate['span']['text'], TEXT.encode()[self.start:self.end].decode())
        self.assertEqual(self.svc.list_claims(), [])
        self.assertEqual(before['fact_count'], self.svc.get_source(self.source['id'], detail=True)['fact_count'])
        approved = self.svc.decide(candidate['id'], 'APPROVE', candidate['etag'])
        evidence = self.imp(b'{"limits":{"max_upload_kb":200}}', 'evidence.json')
        run = self.do_run([approved['claim_revision_id']], [evidence['id']])
        self.assertEqual(run['job']['state'], 'SUCCEEDED', run['job'])
        self.assertEqual(run['findings'][0]['verdict'], 'CONTRADICTED')
        path = self.svc.export_run(run['id'])
        checked = verify_package(path)
        self.assertEqual(checked['status'], 'VERIFIED', checked)
        with zipfile.ZipFile(path) as archive:
            provenance = next(json.loads(archive.read(n)) for n in archive.namelist()
                              if n.startswith('artifacts/') and b'"authoring"' in archive.read(n))
        self.assertEqual(provenance['authoring']['semantic_entailment'], 'not_machine_verified')
        self.assertEqual(provenance['origin_span']['grammar'], 'reviewed-text')
        self.assertIn('exact upload limit', provenance['authoring']['reason'])
        self.assertTrue(any('human_interpretation' in c['name'] for c in checked['checks']))

    def test_candidate_is_not_evidence(self):
        candidate = self.author()
        approved = self.svc.decide(candidate['id'], 'APPROVE', candidate['etag'])
        run = self.do_run([approved['claim_revision_id']], [self.source['id']])
        self.assertEqual(run['findings'][0]['verdict'], 'INSUFFICIENT_EVIDENCE')

    def test_checker_rejects_hidden_human_interpretation_and_rebound_origin(self):
        c = self.author()
        approved = self.svc.decide(c['id'], 'APPROVE', c['etag'])
        ev = self.imp(b'{"limits":{"max_upload_kb":200}}', 'ev.json')
        run = self.do_run([approved['claim_revision_id']], [ev['id']])
        with zipfile.ZipFile(self.svc.export_run(run['id'])) as archive:
            files = {n: archive.read(n) for n in archive.namelist()}
        bundle = json.loads(files['bundle.json'])
        arts = {a['id']: a for a in bundle['artifacts']}
        acts = {a['id']: a for a in bundle['activities']}
        claim = bundle['claims'][0]
        records = _load_records(files, arts, acts, Report())
        prov_path = next(arts[r]['relative_path'] for r in claim['evidence_refs']
                         if arts[r]['media_type'].endswith('claim-provenance+json'))
        baseline = json.loads(files[prov_path])
        for kind in ('hide_reason', 'hide_authoring', 'pretend_extracted', 'different_manifest', 'different_span'):
            prov = copy.deepcopy(baseline)
            if kind == 'hide_reason':
                prov['authoring']['reason'] = ''
            elif kind == 'hide_authoring':
                del prov['authoring']
            elif kind == 'pretend_extracted':
                del prov['authoring']
                prov['origin_span']['grammar'] = 'plain-scalar'
            elif kind == 'different_manifest':
                prov['origin_span']['extraction_manifest_id'] = 'different'
            else:
                prov['origin_span']['locator']['byte_end'] -= 1
            with self.subTest(kind=kind):
                changed = {**files, prov_path: canonical_bytes(prov)}
                rep = Report()
                _check_provenance(claim, changed, arts, rep, records)
                self.assertFalse(rep.ok, rep.checks)

    def test_invalid_spans_and_reasons_leave_no_candidate(self):
        for fields in ({'byte_start': True}, {'byte_start': -1}, {'byte_end': 99999},
                       {'byte_start': 2}, {'byte_start': self.end, 'byte_end': self.end},
                       {'reason': ''}, {'reason': None}, {'source_id': []},
                       {'interpretation': {'predicate': 'exec'}}):
            with self.subTest(fields=fields), self.assertRaises(ServiceError):
                self.author(**fields)
        self.assertEqual(self.svc.list_candidates()['items'], [])

    def test_idempotency_and_reopening_keep_pending_record(self):
        a = self.author(idempotency_key='author-one')
        b = self.author(idempotency_key='author-one')
        self.assertEqual(a['id'], b['id'])
        self.assertEqual(self.svc.list_candidates()['total'], 1)
        with self.assertRaises(ServiceError) as ctx:
            self.author(idempotency_key='author-one', reason='Changed rationale')
        self.assertEqual(ctx.exception.code, 'idempotency_key_reuse')
        other = self.new_service()
        self.assertEqual(other.get_candidate(a['id'])['author_reason'], a['author_reason'])

    def test_non_equality_predicates_can_be_authored_and_reviewed(self):
        for predicate, args in (('fact.present', {}), ('fact.numeric_bound', {'op': 'lte',
                                  'bound': {'kind': 'integer', 'value': '100'}})):
            with self.subTest(predicate=predicate):
                c = self.author(interpretation={'predicate': predicate, 'subject': 'limits.max_upload_kb', 'arguments': args})
                self.assertEqual(self.svc.get_candidate(c['id'])['related'], [])
                self.assertEqual(self.svc.decide(c['id'], 'APPROVE', c['etag'])['status'], 'APPROVED')

    def test_source_replacement_blocks_new_authoring_and_stales_previous_run(self):
        c = self.author()
        a = self.svc.decide(c['id'], 'APPROVE', c['etag'])
        ev = self.imp(b'{"limits":{"max_upload_kb":100}}', 'evidence.json')
        run = self.do_run([a['claim_revision_id']], [ev['id']])
        replacement = self.svc.import_source(b'Replacement prose.', 'prose-v2.md', replaces=self.source['id'])
        self.svc.wait_job(replacement['job_id'])
        self.assertEqual(self.svc.get_run(run['id'])['freshness']['state'], 'STALE')
        with self.assertRaises(ServiceError):
            self.author()


class AuthoredHttp(ServerCase):
    def test_authoring_over_http_preserves_pending_state_and_requires_auth(self):
        status, result, _ = self.call('POST', '/api/v1/sources', TEXT.encode(),
                                    headers={'X-Filename': 'prose.md', 'Content-Type': 'text/plain'})
        self.assertEqual(status, 202)
        self.assertEqual(self.wait(result['job_id'])['state'], 'SUCCEEDED')
        sid = result['source']['id']
        _, source, _ = self.call('GET', '/api/v1/sources/' + quote(sid))
        payload = {'source_id': sid, 'attempt_id': source['attempt']['id'], 'byte_start': 0,
                   'byte_end': len(TEXT.encode()), 'interpretation': INTERPRETATION, 'reason': 'Human reading of the prose.'}
        self.assertEqual(self.call('POST', '/api/v1/candidates', payload, token=False)[0], 401)
        status, candidate, _ = self.call('POST', '/api/v1/candidates', payload)
        self.assertEqual((status, candidate['status']), (201, 'PENDING'))
        self.assertTrue(candidate['authored'])
        self.assertEqual(self.call('POST', '/api/v1/candidates', {'command': 'echo no'})[0], 422)


class AuthoredCli(WorkspaceCase):
    def test_source_inspection_and_authoring_from_cli(self):
        path = self.tmp / 'prose.md'
        path.write_text(TEXT)
        ws = str(self.tmp / 'cli')
        rc, imported, _ = cli('import', str(path), ws=ws)
        self.assertEqual(rc, 0)
        sid = imported[0]['source']['id']
        rc, view, _ = cli('source', sid, ws=ws)
        self.assertEqual(rc, 0)
        self.assertEqual(view['rendition']['text'], TEXT)
        rc, candidate, err = cli('author', sid, '--attempt', view['source']['attempt']['id'],
            '--start', '0', '--end', str(len(TEXT.encode())), '--subject', INTERPRETATION['subject'],
            '--arguments', json.dumps(INTERPRETATION['arguments']), '--reason', 'Interpret written quantity.', ws=ws)
        self.assertEqual(rc, 0, err)
        self.assertEqual(candidate['status'], 'PENDING')
        rc, result, _ = cli('approve', candidate['id'], ws=ws)
        self.assertEqual(rc, 0)
        self.assertTrue(result[0]['claim_revision_id'])
