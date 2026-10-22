"""Portable evidence package: deterministic layout, hash manifest, human report (chapter 16/12)."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

from .canonical import canonical_bytes, sha256_digest
from .model import Bundle

PACKAGE_SCHEMA = "verifier-lab/package/1"
OBSERVATION_FILE = "run_metadata.json"
LIMITATIONS = [
    "A SUPPORTED finding means the evidence agrees with the reviewed claim under the recorded "
    "assumptions and rule versions; it does not establish that either source is true.",
    "Absence of a stated fact is INSUFFICIENT_EVIDENCE, never a negative.",
    "Hashes establish byte consistency only, not authorship, authenticity or completeness.",
    "Reviewer approval records an interpretation of what to test, not that the statement is true.",
    "Extraction from PDF is native-text only; reading order is not verified and OCR is not run.",
    "This package is not a compliance or legal determination.",
]


def write_package(dest: Path, bundle: Bundle, blobs: dict[str, bytes], result: dict, metadata: dict,
                  report_md: str) -> dict:
    """Write to a staging directory and atomically install; returns the manifest."""
    staging = dest.with_name(dest.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    (staging / "artifacts").mkdir(parents=True)
    files: dict[str, bytes] = {"bundle.json": canonical_bytes(bundle.to_dict()),
                               "result.json": canonical_bytes(result),
                               "run_metadata.json": json.dumps(metadata, indent=2, sort_keys=True).encode(),
                               "report.md": report_md.encode("utf-8")}
    for art in bundle.artifacts:
        files[art.relative_path] = blobs[art.digest]
    for rel, data in files.items():
        target = staging / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest = {"schema": PACKAGE_SCHEMA, "bundle_id": bundle.bundle_id,
                "files": [{"path": rel, "sha256": sha256_digest(data), "size": len(data)}
                          for rel, data in sorted(files.items()) if rel != OBSERVATION_FILE],
                "observation_files": [OBSERVATION_FILE]}
    (staging / "MANIFEST.json").write_bytes(canonical_bytes(manifest))
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(staging, dest)
    return manifest


def zip_package(src: Path) -> bytes:
    """Deterministic ZIP: sorted names, fixed timestamps, no extra attributes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(p for p in src.rglob("*") if p.is_file()):
            rel = path.relative_to(src).as_posix()
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    return buf.getvalue()


def render_report(result: dict, claim_view: list[dict]) -> str:
    counts: dict[str, int] = {}
    for f in result["findings"]:
        counts[f["verdict"]] = counts.get(f["verdict"], 0) + 1
    lines = ["# Verification report", "",
             f"Bundle: `{result['bundle_id']}`", "",
             f"Claims evaluated: {result['completion']['claims_completed']} of "
             f"{result['completion']['claims_total']}  ",
             "Execution errors: " + str(len(result["errors"])), "",
             "## Verdict counts", ""]
    for verdict in ("SUPPORTED", "CONTRADICTED", "INSUFFICIENT_EVIDENCE", "INVALID_EVIDENCE",
                    "NOT_APPLICABLE", "HUMAN_REVIEW_REQUIRED"):
        lines.append(f"- {verdict}: {counts.get(verdict, 0)}")
    lines += ["", "## Findings", ""]
    by_claim = {c["claim_id"]: c for c in claim_view}
    for f in result["findings"]:
        c = by_claim.get(f["claim_id"], {})
        lines += [f"### {f['verdict']} - `{c.get('subject', '?')}`", "",
                  f"- Predicate: `{c.get('predicate', '?')}`",
                  f"- Rule: `{f['rule_id']}` v{f['rule_version']}",
                  f"- Reason: `{f['reason_code']}`",
                  f"- Claim: `{f['claim_id']}`", ""]
    if result["errors"]:
        lines += ["## Execution errors", ""]
        for e in result["errors"]:
            lines.append(f"- `{e['claim_id']}`: {e['error']['code']} ({e['error']['operation']})")
        lines.append("")
    lines += ["## Limitations", ""] + [f"- {x}" for x in result["limitations"]]
    lines += ["", "Verify offline with `verifier-lab verify-package <this package>`.", ""]
    return "\n".join(lines)


def runtime_identity() -> dict:
    return {"python": sys.version.split()[0], "platform": sys.platform}
