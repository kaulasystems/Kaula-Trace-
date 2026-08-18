"""Unit tests for the pure pieces: hashing normalisation and redaction."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kaula import hashing, redact  # noqa: E402


class Hashing(unittest.TestCase):
    def test_trailing_whitespace_ignored(self):
        self.assertEqual(hashing.line_hash("foo"), hashing.line_hash("foo   "))
        self.assertEqual(hashing.line_hash("foo\t"), hashing.line_hash("foo"))

    def test_leading_whitespace_significant(self):
        # Indentation is meaningful; do not strip it.
        self.assertNotEqual(hashing.line_hash("  foo"), hashing.line_hash("foo"))

    def test_case_significant(self):
        self.assertNotEqual(hashing.line_hash("Foo"), hashing.line_hash("foo"))

    def test_blank_lines_dropped(self):
        text = "a\n\n   \nb\n"
        self.assertEqual(hashing.line_hashes(text),
                         [hashing.line_hash("a"), hashing.line_hash("b")])

    def test_single_line_matches_block_entry(self):
        block = "  const skew = 30;\nreturn x;\n"
        hs = hashing.line_hashes(block)
        self.assertIn(hashing.line_hash("  const skew = 30;"), hs)

    def test_roundtrip_encoding(self):
        hs = hashing.line_hashes("x\ny\n")
        self.assertEqual(hashing.decode_hashes(hashing.encode_hashes(hs)), hs)


class Redaction(unittest.TestCase):
    def test_aws_key(self):
        out = redact.redact("id AKIAIOSFODNN7EXAMPLE here")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)
        self.assertIn("[REDACTED:aws-access-key]", out)

    def test_assigned_secret_preserves_identifier(self):
        out = redact.redact('password = "hunter2hunter2"')
        self.assertTrue(out.startswith("password"))
        self.assertNotIn("hunter2hunter2", out)
        self.assertIn("[REDACTED:assigned-secret]", out)

    def test_openai_prefix(self):
        out = redact.redact("key sk-abcdEFGH1234abcdEFGH5678 end")
        self.assertIn("[REDACTED:openai-key]", out)

    def test_private_key_block(self):
        blob = ("-----BEGIN PRIVATE KEY-----\nAAAABBBBCCCC\n"
                "-----END PRIVATE KEY-----")
        self.assertIn("[REDACTED:private-key]", redact.redact(blob))

    def test_ordinary_text_untouched(self):
        text = "please refactor the pagination cursor helper"
        self.assertEqual(redact.redact(text), text)


if __name__ == "__main__":
    unittest.main()
