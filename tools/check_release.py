"""Build twice, inspect wheel contents, and exercise an isolated offline installation.

Does not publish, sign, or claim a complete license/security review. Output must be new.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DATA_FILES = {
    'verifier_lab/web/index.html', 'verifier_lab/web/app.js', 'verifier_lab/web/style.css',
    'verifier_lab/riskpacks_data/synthetic-upload-controls.json',
    'verifier_lab/purposes_data/inspect-checkpoint.json', 'verifier_lab/purposes_data/resume-exactly.json',
}
METADATA = {'METADATA', 'WHEEL', 'RECORD', 'entry_points.txt', 'top_level.txt', 'licenses/LICENSE'}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scan_wheel(path: Path, python_files: set[str]) -> list[str]:
    """Exact source-module/data/metadata allowlist; fail closed on extra or missing files."""
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        expected = python_files | DATA_FILES
        metadata_roots = {n.split('/')[0] for n in names if n.split('/')[0].endswith('.dist-info')}
        if len(metadata_roots) != 1:
            raise ValueError('expected exactly one dist-info directory')
        metadata_root = metadata_roots.pop()
        expected |= {f'{metadata_root}/{name}' for name in METADATA}
        if len(names) != len(set(names)) or set(names) != expected:
            raise ValueError(f'wheel allowlist mismatch: extra={sorted(set(names) - expected)}, missing={sorted(expected - set(names))}')
        for info in archive.infolist():
            name = info.filename
            if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
                raise ValueError(f'unsafe wheel path: {name}')
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f'symlink in wheel: {name}')
            data = archive.read(name)
            if b'/workspace/' in data or b'/home/agent/' in data or b'-----BEGIN PRIVATE KEY-----' in data:
                raise ValueError(f'local path or private-key marker in wheel: {name}')
    return sorted(names)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='new directory outside the checkout')
    parser.add_argument('--browser', action='store_true', help='also run the existing Chromium journey from the wheel')
    parser.add_argument('--accessibility', action='store_true', help='require populated-screen WCAG A/AA automated audits')
    args = parser.parse_args()
    if args.accessibility and (not args.browser or not os.environ.get('AXE_MODULE')):
        parser.error('--accessibility requires --browser and AXE_MODULE pointing to axe-core')
    audit_enabled = bool(args.browser and os.environ.get('AXE_MODULE'))
    out = args.output.resolve()
    if out == ROOT or ROOT in out.parents:
        parser.error('output must be outside the source checkout')
    out.mkdir(parents=True, exist_ok=False)
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
    env.update(SOURCE_DATE_EPOCH='1761004800', PYTHONHASHSEED='0', PIP_DISABLE_PIP_VERSION_CHECK='1')
    logs = out / 'logs'
    logs.mkdir()

    def run(name, cmd, cwd=out):
        with (logs / f'{name}.log').open('w') as log:
            result = subprocess.run([str(s) for s in cmd], cwd=cwd, env=env,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=600)
        if result.returncode:
            raise RuntimeError(f'{name} exited {result.returncode}; see {logs / (name + ".log")}')

    # Only hashed wheels are downloaded; all subsequent build/install commands are offline.
    locks = [ROOT / 'tools' / f'release-{kind}-requirements.txt' for kind in ('build', 'pdf')]
    wheelhouse = out / 'wheelhouse'
    run('download', [sys.executable, '-m', 'pip', 'download', '--only-binary=:all:', '--require-hashes',
                     '-r', locks[0], '-r', locks[1], '--dest', wheelhouse])
    build_env = out / 'build-env'
    venv.EnvBuilder(with_pip=True).create(build_env)
    build_python = build_env / 'bin/python'
    run('build-dependencies', [build_python, '-m', 'pip', 'install', '--no-index', '--find-links', wheelhouse,
                               '--require-hashes', '-r', locks[0]])
    sources = sorted(p for p in (ROOT / 'src/verifier_lab').rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts)
    # Describe actual source bytes, including uncommitted changes, rather than claiming HEAD alone.
    inputs = [ROOT / 'pyproject.toml', ROOT / 'LICENSE', *sources, *locks,
              ROOT / 'tools/check_release.py', ROOT / 'tools/release_smoke.py',
              ROOT / 'tests/e2e/browser_journey.cjs', *sorted((ROOT / 'fixtures').rglob('*'))]
    inputs.append(ROOT / 'tests/e2e/accessibility.cjs')
    source_hashes = {str(p.relative_to(ROOT)): digest(p) for p in inputs if p.is_file()}
    python_files = {str(p.relative_to(ROOT / 'src')) for p in sources if p.suffix == '.py'}
    wheels = []
    for index in (1, 2):
        source = out / f'source-{index}'
        source.mkdir()
        shutil.copy2(ROOT / 'pyproject.toml', source)
        shutil.copy2(ROOT / 'LICENSE', source)
        for path in sources:
            target = source / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        dist = out / f'dist-{index}'
        run(f'build-{index}', [build_python, '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                               '--no-index', '--wheel-dir', dist, source])
        built = list(dist.glob('*.whl'))
        if len(built) != 1:
            raise RuntimeError('expected exactly one built wheel')
        wheels.append(built[0])
    names = scan_wheel(wheels[0], python_files)
    scan_wheel(wheels[1], python_files)
    if digest(wheels[0]) != digest(wheels[1]):
        raise RuntimeError('two clean wheel builds differ')
    runtime = out / 'runtime'
    venv.EnvBuilder(with_pip=True).create(runtime)
    python = runtime / 'bin/python'
    run('install-core', [python, '-m', 'pip', 'install', '--no-index', '--no-deps', wheels[0]])
    # Importing the base package must not require the optional PDF parser or the build backend.
    run('core-without-extras', [python, '-I', '-c',
        'import importlib.util, verifier_lab; assert importlib.util.find_spec("pypdf") is None; '
        'assert importlib.util.find_spec("setuptools") is None'])
    run('install-pdf', [python, '-m', 'pip', 'install', '--no-index', '--find-links', wheelhouse,
                        '--require-hashes', '-r', locks[1]])
    run('pip-check', [python, '-m', 'pip', 'check'])
    smoke_root = out / 'acceptance'
    smoke_root.mkdir()
    shutil.copytree(ROOT / 'fixtures', smoke_root / 'fixtures')
    shutil.copy2(ROOT / 'tools/release_smoke.py', smoke_root / 'release_smoke.py')
    run('cli-smoke', [python, '-I', smoke_root / 'release_smoke.py', smoke_root])
    # Start the installed entry point from a directory with no checkout or editable installation.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    url = f'http://127.0.0.1:{port}/'
    with (logs / 'server.log').open('w') as server_log:
        server = subprocess.Popen([str(runtime / 'bin/verifier-lab'), '--workspace', str(out / 'web-workspace'),
                                   'serve', '--port', str(port)], cwd=smoke_root, env=env,
                                  stdout=server_log, stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if server.poll() is not None:
                    raise RuntimeError('installed server exited before readiness')
                try:
                    with urllib.request.urlopen(url, timeout=1) as response:
                        html = response.read()
                        if response.status != 200 or b'/static/app.js' not in html:
                            raise RuntimeError('installed UI response is incomplete')
                    break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                raise RuntimeError('installed server did not become ready')
            for asset in ('app.js', 'style.css'):
                with urllib.request.urlopen(url + 'static/' + asset, timeout=5) as response:
                    if response.status != 200 or not response.read():
                        raise RuntimeError('installed UI asset is missing')
            if args.browser:
                journey = smoke_root / 'tests/e2e/browser_journey.cjs'
                journey.parent.mkdir(parents=True)
                shutil.copy2(ROOT / 'tests/e2e/browser_journey.cjs', journey)
                shutil.copy2(ROOT / 'tests/e2e/accessibility.cjs', journey.parent)
                env['WORKBENCH_URL'] = url
                run('browser', ['node', journey], cwd=smoke_root)
                if audit_enabled:
                    audits = [json.loads(line.removeprefix('accessibility: ')) for line in
                              (logs / 'browser.log').read_text().splitlines() if line.startswith('accessibility: ')]
                    expected_pages = {'import', 'review', 'run', 'findings', 'advanced', 'campaign', 'composition', 'export', 'authoring'}
                    if {a['page'] for a in audits} != expected_pages:
                        raise RuntimeError('required populated accessibility audits did not all execute')
                    (out / 'accessibility.json').write_text(json.dumps(audits, indent=2) + '\n')
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    evidence = {'source_commit': head, 'source_inputs_sha256': source_hashes,
                'source_tree_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
                'python': sys.version, 'platform': sys.platform, 'source_date_epoch': env['SOURCE_DATE_EPOCH'],
                'wheel': wheels[0].name, 'wheel_sha256': digest(wheels[0]), 'wheel_contents': names,
                'dependency_wheels_sha256': {p.name: digest(p) for p in sorted(wheelhouse.glob('*.whl'))},
                'checks': {'matching_clean_builds': True, 'package_allowlist': True, 'core_without_extras': True,
                           'offline_cli_four_formats': True, 'installed_ui_http': True,
                           'automated_accessibility': 'passed' if audit_enabled else 'not_run',
                           'installed_browser_journey': 'passed' if args.browser else 'not_run'},
                'not_verified': ['other platforms', 'full license/provenance review', 'screen-reader audit',
                                 'jurisdiction source review', 'owner publication review']}
    (out / 'evidence.json').write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
    print(f'Release checks passed. Evidence: {out / "evidence.json"}')


if __name__ == '__main__':
    main()
