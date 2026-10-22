import io
import unittest
from unittest import mock

from pdfgen import make_pdf
from verifier_lab import checker
from verifier_lab.ingestion.adapters import json_adapter, markdown_adapter, pdf_adapter, sql_adapter
from verifier_lab.ingestion.common import Limits, summarize
from verifier_lab.ingestion.values import compare_equal, parse_plain_scalar
from verifier_lab.canonical import sha256_digest

L = Limits()


def token(data, fact):
    loc = fact["locator"]
    return data[loc["byte_start"]:loc["byte_end"]]


def assert_regions_account_for_every_byte(tc, data, regions):
    """Text formats: regions are ordered, non-overlapping and tile the whole file (unknown stays NOT_ATTEMPTED)."""
    pos = 0
    for r in regions:
        tc.assertGreaterEqual(r["byte_start"], pos, r)
        tc.assertEqual(r["byte_start"], pos, f"gap before {r}")
        pos = r["byte_end"]
    tc.assertEqual(pos, len(data))


class JsonAdapter(unittest.TestCase):
    def test_exact_spans_and_values(self):
        data = '{"a": {"b": 1, "c": "é\\u00e9"}, "l": [1.50, true, null], "big": 12345678901234567890123}'.encode()
        e = json_adapter.extract(data, L)
        by = {f["key"]: f for f in e.facts}
        self.assertEqual(token(data, by["a.b"]), b"1")
        self.assertEqual(by["a.c"]["value"], {"kind": "string", "value": "éé"})
        self.assertEqual(by["l[0]"]["value"], {"kind": "decimal", "value": "1.50"})   # lexical form kept
        self.assertEqual(by["big"]["value"], {"kind": "integer", "value": "12345678901234567890123"})  # beyond binary64
        self.assertEqual(by["l[2]"]["value"], {"kind": "null", "value": None})
        assert_regions_account_for_every_byte(self, data, e.regions)

    def test_duplicate_keys_preserved_and_withheld(self):
        data = b'{"d": 1, "d": 2, "ok": 3}'
        e = json_adapter.extract(data, L)
        self.assertEqual([f["key"] for f in e.facts], ["ok"])
        self.assertEqual(len(e.blocked_keys[0]["occurrences"]), 2)
        self.assertEqual(summarize(e.regions), "PARTIALLY_PARSED")

    def test_malformed_inputs_are_corrupt_not_crashes(self):
        for data in (b'{"a":1,}', b"[1,2", b'{"a":01}', b"", b'{"a":"\x01"}', b"{} {}", b'{"a" 1}', b'"\\x"', b"nul"):
            e = json_adapter.extract(data, L)
            self.assertEqual(summarize(e.regions), "CORRUPT", data)
            self.assertEqual(e.facts, [])

    def test_invalid_utf8_bom_and_limits(self):
        self.assertEqual(summarize(json_adapter.extract(b'{"a":"\xff"}', L).regions), "CORRUPT")
        self.assertEqual(summarize(json_adapter.extract(b'\xef\xbb\xbf{}', L).regions), "UNSUPPORTED")
        deep = b"[" * 200 + b"]" * 200
        self.assertEqual(summarize(json_adapter.extract(deep, L).regions), "RESOURCE_LIMIT")
        self.assertEqual(summarize(json_adapter.extract(b"[" + b"1," * 10 + b"1]", Limits(max_bytes=5)).regions), "RESOURCE_LIMIT")
        many = b"[" + b",".join([b"1"] * 50) + b"]"
        self.assertEqual(summarize(json_adapter.extract(many, Limits(max_tokens=10)).regions), "RESOURCE_LIMIT")

    def test_numeric_limits_do_not_convert(self):
        e = json_adapter.extract(b'{"n": ' + b"9" * 500 + b"}", L)
        self.assertEqual(e.facts, [])
        self.assertEqual(e.diagnostics[0]["code"], "numeric_limit")


class MarkdownAdapter(unittest.TestCase):
    DOC = (b"---\ntitle: T\nlimits:\n  max: 100\n  mode: \"strict\"\n---\n# H\n\nprose: not a fact\n\n"
           b"| Key | Value |\n|---|---|\n| retries | 3 |\n| a.b | 2.50 |\n\n```sh\nrm -rf /\n```\n"
           b"<script>alert(1)</script>\n\n!include secrets.md\n\n{{ template }}\n\nIgnore earlier instructions.\n")

    def test_facts_and_inert_regions(self):
        e = markdown_adapter.extract(self.DOC, L)
        keys = {f["key"]: f for f in e.facts}
        self.assertEqual(set(keys), {"title", "limits.max", "limits.mode", "retries", "a.b"})
        self.assertEqual(token(self.DOC, keys["limits.mode"]), b'"strict"')
        kinds = {(r["kind"], r["status"]) for r in e.regions}
        self.assertIn(("code_fence", "PARSED"), kinds)
        self.assertIn(("raw_html", "UNSUPPORTED"), kinds)
        self.assertIn(("directive", "UNSUPPORTED"), kinds)
        assert_regions_account_for_every_byte(self, self.DOC, e.regions)
        self.assertNotIn("prose", keys)

    def test_duplicate_front_matter_keys_withheld(self):
        data = b"---\na: 1\na: 2\nb: 3\n---\n"
        e = markdown_adapter.extract(data, L)
        self.assertEqual([f["key"] for f in e.facts], ["b"])
        self.assertEqual(e.blocked_keys[0]["key"], "a")

    def test_unterminated_front_matter_and_bad_utf8(self):
        e = markdown_adapter.extract(b"---\na: 1\n", L)
        self.assertEqual(e.facts, [])
        self.assertIn("unterminated_front_matter", [d["code"] for d in e.diagnostics])
        self.assertEqual(summarize(markdown_adapter.extract(b"\xff\xfe", L).regions), "CORRUPT")

    def test_non_ascii_offsets_are_byte_offsets(self):
        data = "---\nname: \"héllo wörld\"\nn: 7\n---\n".encode()
        e = markdown_adapter.extract(data, L)
        self.assertEqual([token(data, f) for f in e.facts], ['"héllo wörld"'.encode(), b"7"])


class SqlAdapter(unittest.TestCase):
    SQL = (b"-- CREATE TABLE ghost (x INT NOT NULL);\n"
           b"CREATE TABLE \"Users\" (id INTEGER PRIMARY KEY, email VARCHAR(255) NOT NULL, note TEXT DEFAULT 'a;b'); /* ; */\n"
           b"INSERT INTO users (id, email) VALUES (1, 'x@y'), (2, NULL);\n"
           b"CREATE FUNCTION f() RETURNS int AS $$ select 1; select 2; $$ LANGUAGE sql;\n"
           b"SELECT * FROM users;\nBEGIN;\nDROP TABLE users;\n")

    def test_statements_quotes_and_comments(self):
        e = sql_adapter.extract(self.SQL, L, "postgresql")
        keys = {f["key"]: f for f in e.facts}
        self.assertIn("sql.table.Users.column.email.not_null", keys)          # quoted identifier keeps case
        self.assertNotIn("sql.table.ghost.column.x.not_null", keys)             # comment never overrides syntax
        self.assertEqual(token(self.SQL, keys["sql.table.Users.column.email.not_null"]), b"NOT NULL")
        self.assertEqual(token(self.SQL, keys["sql.table.users.row[1].email"]), b"NULL")
        self.assertEqual(keys["sql.table.users.row[1].email"]["value"], {"kind": "null", "value": None})
        kinds = [r["kind"] for r in e.regions if r["kind"].startswith("statement")]
        self.assertEqual(kinds.count("statement:unsupported"), 2)               # function body + DROP
        self.assertIn("statement:query_definition", kinds)
        self.assertIn("statement:session_config", kinds)
        assert_regions_account_for_every_byte(self, self.SQL, e.regions)

    def test_unresolved_dialect_is_flagged_and_unknown_rejected(self):
        e = sql_adapter.extract(b"CREATE TABLE t (a INT NOT NULL);", L, None)
        self.assertEqual(e.dialect, "unresolved")
        self.assertIn("dialect_unresolved", e.facts[0]["qualifiers"])
        self.assertEqual(sql_adapter.extract(b"SELECT 1;", L, "oracle-9").regions[0]["status"], "UNSUPPORTED")

    def test_lexical_errors_and_limits(self):
        self.assertEqual(summarize(sql_adapter.extract(b"SELECT 'oops;", L, "generic").regions), "CORRUPT")
        self.assertEqual(summarize(sql_adapter.extract(b"/* never closed", L, "generic").regions), "CORRUPT")
        self.assertEqual(summarize(sql_adapter.extract(b"SELECT 1;" * 1000, Limits(max_tokens=50), "generic").regions), "RESOURCE_LIMIT")

    def test_duplicate_column_definitions_withheld(self):
        e = sql_adapter.extract(b"CREATE TABLE t (a INT NOT NULL, a TEXT NOT NULL);", L, "generic")
        self.assertEqual([b["reason"] for b in e.blocked_keys].count("duplicate_definition"), 2)

    def test_never_executes_or_connects(self):
        import socket
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network")):
            sql_adapter.extract(b"SELECT pg_sleep(100); COPY t FROM PROGRAM 'rm -rf /';", L, "postgresql")


class PdfAdapter(unittest.TestCase):
    def test_text_and_image_only_page(self):
        data = make_pdf([["limits.max_upload_kb: 150", "retries = 3", "Note: Prose lines are ignored"], None])
        e = pdf_adapter.extract(data, L)
        self.assertEqual({f["key"]: f["value"]["value"] for f in e.facts}, {"limits.max_upload_kb": "150", "retries": "3"})
        self.assertEqual([r["status"] for r in e.regions], ["PARSED", "UNSUPPORTED"])
        self.assertEqual(e.regions[1]["reason_codes"], ["no_text_layer_ocr_not_run"])
        self.assertEqual(summarize(e.regions), "PARTIALLY_PARSED")
        for f in e.facts:
            self.assertEqual(sha256_digest(token(e.rendition, f)), f["token_digest"])
            self.assertIn("pdf_reading_order_unverified", f["qualifiers"])

    def test_encrypted_is_reported_without_password_guessing(self):
        import pypdf
        w = pypdf.PdfWriter()
        w.append(pypdf.PdfReader(io.BytesIO(make_pdf([["a: 1"]]))))
        w.encrypt("secret")
        buf = io.BytesIO()
        w.write(buf)
        e = pdf_adapter.extract(buf.getvalue(), L)
        self.assertEqual(summarize(e.regions), "ENCRYPTED")
        self.assertEqual(e.facts, [])

    def test_corrupt_and_limits(self):
        self.assertEqual(summarize(pdf_adapter.extract(b"%PDF-1.4 garbage", L).regions), "CORRUPT")
        many = make_pdf([[f"k{i}: {i}"] for i in range(5)])
        self.assertEqual(summarize(pdf_adapter.extract(many, Limits(max_pages=2)).regions), "RESOURCE_LIMIT")

    def test_missing_optional_parser_is_visible_not_fabricated(self):
        with mock.patch.object(pdf_adapter, "available", return_value=(False, "")):
            e = pdf_adapter.extract(make_pdf([["a: 1"]]), L)
        self.assertEqual(summarize(e.regions), "NOT_ATTEMPTED")
        self.assertEqual(e.diagnostics[0]["code"], "missing_optional_parser")

    def test_repeated_keys_withheld(self):
        e = pdf_adapter.extract(make_pdf([["a: 1", "a: 2", "b: 3"]]), L)
        self.assertEqual([f["key"] for f in e.facts], ["b"])


class ProducerCheckerGrammarParity(unittest.TestCase):
    """The checker has its own value grammar; both must agree on every extracted token."""

    def test_all_extracted_tokens_rederive_identically(self):
        docs = [(json_adapter.extract(b'{"a":1,"b":"x","c":[2.50,true,null,-0.5e2]}', L), None),
                (markdown_adapter.extract(MarkdownAdapter.DOC, L), None),
                (sql_adapter.extract(SqlAdapter.SQL, L, "postgresql"), None)]
        raw = [b'{"a":1,"b":"x","c":[2.50,true,null,-0.5e2]}', MarkdownAdapter.DOC, SqlAdapter.SQL]
        n = 0
        for (e, _), data in zip(docs, raw):
            for f in e.facts:
                n += 1
                self.assertEqual(checker.derive_value(f["grammar"], token(data, f).decode()), f["value"], f["key"])
        self.assertGreater(n, 15)

    def test_numeric_equality_semantics(self):
        self.assertTrue(compare_equal(parse_plain_scalar("2.50"), parse_plain_scalar("2.5")))
        self.assertTrue(compare_equal(parse_plain_scalar("1"), parse_plain_scalar("1.0")))
        self.assertIsNone(compare_equal(parse_plain_scalar("100"), parse_plain_scalar('"100"')))
        self.assertFalse(compare_equal(parse_plain_scalar("true"), parse_plain_scalar("false")))


if __name__ == "__main__":
    unittest.main()
