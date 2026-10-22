import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from verifier_lab.canonical import (CanonicalError, canonical_bytes, parse_digest, record_id,
                                    sha256_digest, strict_loads)

ROOT = Path(__file__).resolve().parents[2]


class CanonicalVectors(unittest.TestCase):
    def test_golden_bytes_and_ids(self):
        value = {"b": [1, {"z": None, "a": True}], "a": "é", "c": -7}
        self.assertEqual(canonical_bytes(value), '{"a":"é","b":[1,{"a":true,"z":null}],"c":-7}'.encode())
        self.assertEqual(record_id("claim", {"x": 1}),
                         "claim:sha256:" + __import__("hashlib").sha256(b'claim\x00{"x":1}').hexdigest())
        # frozen vector: any change to canonical encoding or domain separation must be a new version
        self.assertEqual(record_id("claim", {"x": 1}), "claim:sha256:c4c79745567cd57fc9ae2070450be0b40251f9fb30110e0db0d1474f0dd0e124")

    def test_domain_separation(self):
        self.assertNotEqual(record_id("claim", {"a": 1}), record_id("artifact", {"a": 1}))

    def test_key_order_does_not_change_identity_but_list_order_does(self):
        self.assertEqual(record_id("x", {"a": 1, "b": 2}), record_id("x", {"b": 2, "a": 1}))
        self.assertNotEqual(record_id("x", {"l": [1, 2]}), record_id("x", {"l": [2, 1]}))

    def test_floats_and_nonfinite_rejected(self):
        for bad in (b'{"a": 1.5}', b'{"a": NaN}', b'{"a": Infinity}', b'{"a": 1e3}'):
            with self.assertRaises(CanonicalError, msg=bad):
                strict_loads(bad)
        with self.assertRaises(CanonicalError):
            canonical_bytes({"a": 1.0})

    def test_duplicate_keys_rejected(self):
        with self.assertRaises(CanonicalError) as cm:
            strict_loads(b'{"a": 1, "a": 2}')
        self.assertEqual(cm.exception.code, "duplicate_key")

    def test_utf8_bom_depth_and_size(self):
        for data, code in ((b'\xff{}', "invalid_utf8"), (b'\xef\xbb\xbf{}', "bom_forbidden"),
                           (b'[' * 500 + b']' * 500, "depth_exceeded")):
            with self.assertRaises(CanonicalError) as cm:
                strict_loads(data)
            self.assertEqual(cm.exception.code, code)
        with self.assertRaises(CanonicalError) as cm:
            strict_loads(b'[' + b'1,' * 100 + b'1]', max_bytes=50)
        self.assertEqual(cm.exception.code, "size_limit")

    def test_unpaired_surrogate_rejected(self):
        with self.assertRaises(CanonicalError):
            canonical_bytes({"a": "\ud800"})

    def test_unicode_preserved_not_normalised(self):
        composed, decomposed = "\u00e9", "e\u0301"
        self.assertNotEqual(record_id("x", {"k": composed}), record_id("x", {"k": decomposed}))

    def test_digest_syntax(self):
        self.assertTrue(parse_digest(sha256_digest(b"x")))
        self.assertFalse(parse_digest("sha256:ABC"))
        self.assertFalse(parse_digest("md5:" + "0" * 32))

    def test_identity_stable_across_hash_seeds(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "from verifier_lab.canonical import record_id;"
                "print(record_id('claim', {'k': list('abc'), 'm': {'z': 1, 'a': 2}, 's': 'ü'}))")
        outs = set()
        for seed in ("0", "1", "12345", "random"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            outs.add(subprocess.run([sys.executable, "-c", code, str(ROOT / "src")], env=env, capture_output=True,
                                    text=True, check=True).stdout)
        self.assertEqual(len(outs), 1)


if __name__ == "__main__":
    unittest.main()
