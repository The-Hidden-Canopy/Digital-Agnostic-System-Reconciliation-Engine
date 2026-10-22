"""Point a campaign at YOUR verifier program (chapter 09: a campaign must be able to judge something other than ourselves).

Protocol (JSON lines, one fresh process per scenario so cases cannot influence each other):
  request : {"cmd":"verify","scenario":{id, origin{name,fmt,text,sha256}, evidence[{...}], claims[{key,predicate,subject,arguments}]}}
  response: {"ok":true,"outcomes":{"<claim key>":["VERDICT","reason_code"], ...}}
The six verdicts are SUPPORTED, CONTRADICTED, INSUFFICIENT_EVIDENCE, INVALID_EVIDENCE, NOT_APPLICABLE, HUMAN_REVIEW_REQUIRED.
Like subject programs, this is configured on the command line only and is contained, not sandboxed.
"""
from __future__ import annotations

from ..subjects.external import ExternalSubject, SubjectError, subject_identity
from .world import DOMAIN_VERDICTS_LIST, Scenario


class ExternalKernel:
    def __init__(self, command: list[str], env: dict | None = None, timeout: float = 20.0):
        from pathlib import Path
        self.command = [str(Path(c).resolve()) if Path(c).is_file() else c for c in command]
        self.env, self.timeout = dict(env or {}), timeout
        self.identity = subject_identity(self.command, self.env)
        self.name = "external:" + self.identity[7:23]

    def verify(self, scenario: Scenario) -> dict:
        proc = ExternalSubject(self.command, "", self.timeout, self.env, handshake=False)
        try:
            resp = proc._ok({"cmd": "verify", "scenario": scenario.to_dict()})
        finally:
            proc.close()
        out = resp.get("outcomes")
        if not isinstance(out, dict):
            raise SubjectError("protocol_violation", "response needs an 'outcomes' object")
        result = {}
        for k, v in out.items():
            if not (isinstance(v, list) and len(v) == 2 and v[0] in DOMAIN_VERDICTS_LIST and isinstance(v[1], str)):
                raise SubjectError("protocol_violation", f"outcome for {k!r} must be [verdict, reason]")
            result[k] = (v[0], v[1])
        return result
