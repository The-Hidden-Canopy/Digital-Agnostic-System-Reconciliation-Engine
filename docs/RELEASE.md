# Reproducible release checks

The current validation target is Linux with Python 3.12, Bubblewrap, libseccomp2 and enabled user namespaces. This is a release-check workflow, not a
publication command. Other platforms are not established by one Linux run. No signing or upload
is performed by the helper; GitHub CI retains its artifacts for review.

On Debian/Ubuntu, install `bubblewrap libseccomp2` through the OS package manager first. A host
that prohibits user-namespace UID mapping cannot execute the required parser integration gate;
use the host execution surface rather than bypassing isolation in tests.

From the repository root, using Python 3.12 with `venv` and pip available:

```bash
python3 -m venv /tmp/dasre-tests
/tmp/dasre-tests/bin/python -m pip install --require-hashes \
  -r tools/release-build-requirements.txt -r tools/release-pdf-requirements.txt
/tmp/dasre-tests/bin/python -m pip install --no-deps --no-build-isolation -e .
/tmp/dasre-tests/bin/python -m unittest discover -s tests -t .
/tmp/dasre-tests/bin/python tools/check_release.py --output /tmp/dasre-release
```

Choose a new output path for each run, outside the checkout. The helper refuses to replace existing
output. It downloads only hash-pinned Python wheels, then builds and installs with `--no-index`.
The network is used to acquire dependencies initially; this is not a test that blocks all sockets.
Application smoke checks need no external services or credentials. The two build directories and
fresh runtime virtualenv are kept for inspection and are never added to the distribution.

For installed-wheel UI acceptance, add `--browser`. Node and Playwright must already be installed.
The CI workflow installs Playwright 1.62.1 and its Chromium runtime. In the cloud development image:

```bash
CHROMIUM_PATH=/usr/bin/chromium /workspace/.venvs/dasre/bin/python tools/check_release.py \
  --output /tmp/dasre-release-with-browser --browser
```

When the copied journey cannot resolve Playwright, set `PLAYWRIGHT_MODULE` to its installed absolute
module path. `CHROMIUM_PATH` is optional when using Playwright's own downloaded browser. Browser
acceptance always uses a fresh application workspace. The helper terminates its server on success
or failure. The journey is copied alongside synthetic fixtures outside the checkout and accesses
the installed wheel's UI, not an editable application installation.

For the automated WCAG A/AA gate, install axe-core 4.11.0 as a test-only dependency and set
`AXE_MODULE` to its module directory, then add `--accessibility` alongside `--browser`. The gate
requires all nine populated-screen audits to execute and verifies that an intentionally unlabeled
button fails the audit. `accessibility.json` retains the engine version, passing-rule counts and
checks that need manual review. No rule, element or result category is excluded to obtain a pass.
Automated coverage supplements the keyboard journey; it does not replace a screen-reader audit.

### Linux check from another OS

With Docker (and Bash, such as Git Bash), `tools/linux_container_check.sh unit` runs the full suite and
`tools/linux_container_check.sh release` runs the clean-build, installed-wheel Chromium and axe gate inside a
privileged Ubuntu 24.04 container, with Bubblewrap and libseccomp in required mode. It tests the committed `HEAD`
(commit first), downloads Node, Playwright and Chromium for `release`, and is local validation only, not a remote CI run.

## Evidence and limits

A successful run writes `evidence.json`, build wheels in `dist-1/` and `dist-2/`, input/dependency
hashes, the source commit and dirty-state indicator, and per-command logs in `logs/`. For an
uncommitted tree, the input digest map identifies the bytes used; the commit alone does not.
Failures propagate a nonzero exit code and retain logs; no success manifest is emitted before all
requested checks pass. The full unittest suite runs separately in CI and must also pass.

The content gate permits Python modules from `src/verifier_lab`, six explicitly named static/catalog
files and known distribution metadata. It rejects extras, missing members, duplicates, unsafe paths,
symlinks and selected path/key markers. It does not establish that arbitrary source text is free of
secrets, that dependencies are vulnerability-free, or that every license obligation is fulfilled.
Semantic export reproducibility is covered by the existing workflow tests; byte-identical wheel
builds are checked separately here. A second operating system or toolchain is a new validation run.

## Dependency inventory for this check

| Dependency | Role | Pinned version | License reported by wheel metadata |
|---|---|---|---|
| setuptools | Build only | 80.9.0 | MIT |
| wheel | Build only | 0.45.1 | MIT classifier |
| pypdf | Optional PDF extraction | 6.21.2 | BSD-3-Clause |
| Playwright | Browser tests only | 1.62.1 | Apache-2.0 package metadata |
| axe-core | Accessibility tests only | 4.11.0 | MPL-2.0 package metadata |
| Chromium | Browser tests only | Image or Playwright runtime | Not reviewed in this increment |

The Python wheel hashes are in `tools/release-*-requirements.txt`. These pins are for release
validation; `pyproject.toml` retains the normal optional parser dependency contract. The shipped
application uses Apache-2.0 as already recorded in `LICENSE` and `OPEN_CANOPY_CONTRACT.md`.
This inventory records metadata, not a completed provenance, notice, vulnerability or legal review.
Before publication, review the actual artifacts, dependency notices and public file set with the
owner; complete outstanding accessibility and scope gates listed in [BUILD_PROGRESS.md](BUILD_PROGRESS.md).
