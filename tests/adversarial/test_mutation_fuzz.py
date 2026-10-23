"""Seeded, bounded mutation fuzzing of the four format adapters.

Every mutant of a real fixture must be handled as data: no exception escapes, the result is
deterministic, fact locators stay inside the rendition, and text formats still account for every byte.
This is a bounded regression campaign (fixed seeds, fixed counts), not a claim of exhaustive fuzzing.
"""
import random
import unittest
from pathlib import Path

from verifier_lab.ingestion.adapters import json_adapter, markdown_adapter, pdf_adapter, sql_adapter
from verifier_lab.ingestion.common import Limits

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "tutorial"
L = Limits()
SEED = 20261023


def mutate(rng: random.Random, data: bytes) -> bytes:
    b = bytearray(data)
    for _ in range(rng.randint(1, 4)):
        kind = rng.choice(("flip", "insert", "delete", "truncate", "dup", "splice", "special"))
        pos = rng.randrange(len(b) + 1)
        if kind == "flip" and b:
            b[min(pos, len(b) - 1)] ^= 1 << rng.randrange(8)
        elif kind == "insert":
            b[pos:pos] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
        elif kind == "delete" and b:
            del b[pos:pos + rng.randint(1, 16)]
        elif kind == "truncate":
            del b[pos:]
        elif kind == "dup" and b:
            a = rng.randrange(len(b))
            b[pos:pos] = b[a:a + rng.randint(1, 64)]
        elif kind == "splice" and b:
            a = rng.randrange(len(b))
            b[pos:pos + rng.randint(0, 8)] = b[a:a + rng.randint(1, 8)]
        elif kind == "special":
            b[pos:pos] = rng.choice((b"\x00", b"\xff\xfe", b"\xef\xbb\xbf", b"\xc3", b"\xe2\x82", b"\r\n", b"\r",
                                     b"[" * 300, b'{"a":' * 200, b'"', b"\\u00", b"--", b"/*", b"'", b"$$"))
        if len(b) > 200_000:
            del b[200_000:]
    return bytes(b)


def tiles(data: bytes, regions: list[dict]) -> bool:
    pos = 0
    for r in regions:
        if r["byte_start"] != pos or r["byte_end"] < r["byte_start"]:
            return False
        pos = r["byte_end"]
    return pos == len(data)


def covers_with_nesting(data: bytes, regions: list[dict]) -> bool:
    """SQL reports an interior comment as a region nested in its statement: every byte covered, overlap only by containment."""
    spans = sorted(((r["byte_start"], r["byte_end"]) for r in regions), key=lambda s: (s[0], -s[1]))
    reach, stack = 0, []
    for a, b in spans:
        while stack and stack[-1] <= a:
            stack.pop()
        if stack and b > stack[-1]:
            return False                      # partial overlap, not nesting
        if a > reach:
            return False                      # uncovered bytes
        reach = max(reach, b)
        stack.append(b)
    return reach == len(data)


class MutationFuzz(unittest.TestCase):
    def campaign(self, name, extract, count, accounting=tiles):
        base = (FIXTURES / name).read_bytes()
        rng = random.Random(f"{SEED}:{name}")
        for n in range(count):
            data = mutate(rng, base)
            case = f"{name} mutant {n} ({len(data)} bytes, sha prefix {data[:12].hex()})"
            try:
                first = extract(data)
                second = extract(data)
            except Exception as exc:                                   # noqa: BLE001 - any escape is the failure
                self.fail(f"{case}: {type(exc).__name__}: {exc}")
            self.assertEqual((first.facts, first.regions, first.diagnostics), (second.facts, second.regions, second.diagnostics), case)
            size = len(first.rendition if first.rendition is not None else data)
            for f in first.facts:
                loc = f["locator"]
                self.assertTrue(0 <= loc["byte_start"] <= loc["byte_end"] <= size, f"{case}: span {loc}")
            if accounting:
                self.assertTrue(accounting(data, first.regions), f"{case}: regions do not account for every byte")

    def test_json(self):
        self.campaign("service-config.json", lambda d: json_adapter.extract(d, L), 400)

    def test_markdown(self):
        self.campaign("product-spec.md", lambda d: markdown_adapter.extract(d, L), 400)

    def test_sql(self):
        for dialect in (None, "postgresql"):
            self.campaign("schema.sql", lambda d: sql_adapter.extract(d, L, dialect), 250, covers_with_nesting)

    def test_pdf(self):
        try:
            import pypdf  # noqa: F401
        except ImportError:
            self.skipTest("optional pypdf not installed")
        self.campaign("ops-note.pdf", lambda d: pdf_adapter.extract(d, L), 120, accounting=None)


if __name__ == "__main__":
    unittest.main()
