"""Run from outside the checkout with an installed wheel; no producer imports here."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def smoke(root: Path) -> dict:
    workspace = root / 'cli-workspace'
    fixtures = root / 'fixtures' / 'tutorial'
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}

    def cli(*args: str, expected: int = 0):
        result = subprocess.run([sys.executable, '-I', '-m', 'verifier_lab.cli',
                                 '--workspace', str(workspace), *args],
                                env=env, cwd=root, capture_output=True, text=True, timeout=60)
        if result.returncode != expected:
            raise RuntimeError(f'{args[0]}: exit {result.returncode}, expected {expected}: {result.stderr}')
        return json.loads(result.stdout)

    imported = cli('import', *(str(fixtures / name) for name in
                              ('product-spec.md', 'service-config.json', 'ops-note.pdf')))
    spec, config, pdf = [item['source']['id'] for item in imported]
    sql = cli('import', str(fixtures / 'schema.sql'), '--dialect', 'postgresql')[0]['source']['id']
    candidates = cli('candidates', '--source', spec)['items']
    keys = {'limits.max_upload_kb': 'CONTRADICTED', 'limits.retries': 'SUPPORTED',
            'timeout.seconds': 'SUPPORTED', 'region': 'SUPPORTED',
            'audit.enabled': 'INSUFFICIENT_EVIDENCE', 'sql.table.users.column.email.not_null': 'SUPPORTED'}
    selected = [c['id'] for c in candidates if c['fact_key'] in keys]
    if len(selected) != len(keys):
        raise RuntimeError('tutorial claim set differs from the committed expectations')
    cli('approve', *selected)
    run = cli('check', '--evidence', config, sql, expected=1)
    findings = {f['claim']['subject']: f['verdict'] for f in run['findings']}
    if findings != keys:
        raise RuntimeError(f'tutorial findings differ: {findings}')
    package = root / 'tutorial.zip'
    cli('export', run['id'], '--output', str(package))
    verified = cli('verify-package', str(package))
    if verified['status'] != 'VERIFIED':
        raise RuntimeError('independent checker did not verify tutorial package')
    # A PDF claim must travel through review, comparison and independent checking as well.
    pdf_candidates = cli('candidates', '--source', pdf)['items']
    selected = [c['id'] for c in pdf_candidates if c['fact_key'] == 'limits.max_upload_kb']
    if len(selected) != 1:
        raise RuntimeError('PDF parser did not extract the expected claim')
    revision = cli('approve', *selected)[0]['claim_revision_id']
    pdf_run = cli('check', '--claims', revision, '--evidence', config, expected=1)
    if [f['verdict'] for f in pdf_run['findings']] != ['CONTRADICTED']:
        raise RuntimeError('PDF claim must contradict the JSON config')
    pdf_package = root / 'pdf.zip'
    cli('export', pdf_run['id'], '--output', str(pdf_package))
    if cli('verify-package', str(pdf_package))['status'] != 'VERIFIED':
        raise RuntimeError('PDF package was not independently verified')
    return {'formats': ['Markdown', 'JSON', 'SQL', 'PDF'], 'tutorial_findings': findings,
            'pdf_verdict': 'CONTRADICTED', 'independent_packages_verified': 2}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    print(json.dumps(smoke(parser.parse_args().root), indent=2, sort_keys=True))
