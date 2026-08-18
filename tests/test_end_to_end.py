"""End-to-end: simulate harness hooks, commit, then attribute a line.

Runs the real CLI (`python -m kaula`) against a throwaway git repo and a
throwaway KAULA_HOME, so the hook → spool → drain → bind → why pipeline is
exercised exactly as it would be in the field.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "proj"
        self.repo.mkdir()
        self.home = root / "kaula-home"
        self.env = dict(os.environ)
        self.env["KAULA_HOME"] = str(self.home)
        self.env["PYTHONPATH"] = str(REPO)
        self.env["GIT_AUTHOR_NAME"] = "Test"
        self.env["GIT_AUTHOR_EMAIL"] = "test@example.com"
        self.env["GIT_COMMITTER_NAME"] = "Test"
        self.env["GIT_COMMITTER_EMAIL"] = "test@example.com"
        self._git("init", "-q")
        self._git("config", "commit.gpgsign", "false")

    def tearDown(self):
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _kaula(self, *args, stdin=None):
        proc = subprocess.run(
            [sys.executable, "-m", "kaula", *args],
            cwd=self.repo, env=self.env,
            input=stdin.encode() if stdin else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return proc

    def _hook(self, harness, event, payload):
        return self._kaula("hook", harness, event, stdin=json.dumps(payload))

    # -- the test --------------------------------------------------------
    def test_full_pipeline(self):
        sid = "sess-abc123"
        cwd = str(self.repo)

        self._hook("cc", "session-start",
                   {"session_id": sid, "cwd": cwd, "model": "claude-opus-5"})
        self._hook("cc", "prompt",
                   {"session_id": sid, "cwd": cwd,
                    "prompt": "make the token refresh handle clock skew"})

        content = (
            "export function refresh(token: string) {\n"
            "  const skew = 30;\n"
            "  return validate(token, skew);\n"
            "}\n"
        )
        (self.repo / "auth.ts").write_text(content)
        self._hook("cc", "edit", {
            "session_id": sid, "cwd": cwd,
            "tool_name": "Write",
            "tool_input": {"file_path": str(self.repo / "auth.ts"), "content": content},
        })
        self._hook("cc", "session-end", {"session_id": sid, "cwd": cwd})

        self._kaula("drain")

        # Commit and bind.
        self._git("add", "auth.ts")
        self._git("commit", "-q", "-m", "fix: tolerate clock skew")
        bind = self._kaula("hook", "git", "post-commit")
        self.assertEqual(bind.returncode, 0)

        # Attribute a distinctive line (line 2: "const skew = 30;").
        out = self._kaula("why", "auth.ts:2", "--json")
        self.assertEqual(out.returncode, 0, out.stderr.decode())
        data = json.loads(out.stdout.decode())
        self.assertEqual(data["confidence"], "exact")
        self.assertIsNotNone(data["prompt"])
        self.assertIn("clock skew", data["prompt"]["text"])
        self.assertEqual(data["session"]["model"], "claude-opus-5")
        self.assertIsNotNone(data["commit"])

    def test_human_line_after_agent_commit(self):
        sid = "sess-human"
        cwd = str(self.repo)
        self._hook("cc", "session-start", {"session_id": sid, "cwd": cwd})
        self._hook("cc", "prompt", {"session_id": sid, "cwd": cwd, "prompt": "add a helper"})
        content = "def helper():\n    return 1\n"
        (self.repo / "m.py").write_text(content)
        self._hook("cc", "edit", {
            "session_id": sid, "cwd": cwd, "tool_name": "Write",
            "tool_input": {"file_path": str(self.repo / "m.py"), "content": content},
        })
        self._kaula("drain")
        self._git("add", "m.py")
        self._git("commit", "-q", "-m", "add helper")
        self._kaula("hook", "git", "post-commit")

        # A human appends a line and amends — content never seen by an agent.
        (self.repo / "m.py").write_text(content + "# hand-written note\n")
        self._git("add", "m.py")
        self._git("commit", "-q", "--amend", "--no-edit")
        self._kaula("hook", "git", "post-rewrite",
                    stdin="")  # amend triggers post-rewrite in real git; ok if empty here
        self._kaula("hook", "git", "post-commit")

        out = self._kaula("why", "m.py:3", "--json")
        data = json.loads(out.stdout.decode())
        # The agent line still resolves; the hand-written line must not be 'exact'.
        self.assertIn(data["confidence"], ("commit", "human", "unattributed", "ambiguous"))

    def test_search_and_sessions(self):
        sid = "sess-search"
        cwd = str(self.repo)
        self._hook("cc", "session-start", {"session_id": sid, "cwd": cwd})
        self._hook("cc", "prompt",
                   {"session_id": sid, "cwd": cwd, "prompt": "refactor the pagination cursor"})
        self._kaula("drain")

        s = self._kaula("search", "pagination", "--json")
        self.assertEqual(s.returncode, 0, s.stderr.decode())
        rows = json.loads(s.stdout.decode())
        self.assertTrue(any("pagination" in (r["snip"] or "") for r in rows))

        ss = self._kaula("sessions", "--json")
        sessions = json.loads(ss.stdout.decode())
        self.assertTrue(any(x["session"]["session_id"] == sid for x in sessions))

    def test_ambiguous_duplicated_line(self):
        """Two sessions edit the same file before one commit; a line they share
        resolves to `ambiguous`, never inflated to `exact`."""
        cwd = str(self.repo)
        # Session A writes the first version.
        self._hook("cc", "session-start", {"session_id": "A", "cwd": cwd})
        self._hook("cc", "prompt", {"session_id": "A", "cwd": cwd, "prompt": "scaffold guard"})
        v1 = "def a():\n    return None\n"
        (self.repo / "d.py").write_text(v1)
        self._hook("cc", "edit", {"session_id": "A", "cwd": cwd, "tool_name": "Write",
                                  "tool_input": {"file_path": str(self.repo / "d.py"), "content": v1}})
        # Session B rewrites it, keeping the same `return None` line.
        self._hook("cc", "session-start", {"session_id": "B", "cwd": cwd})
        self._hook("cc", "prompt", {"session_id": "B", "cwd": cwd, "prompt": "add second guard"})
        v2 = "def a():\n    return None\n\ndef b():\n    return None\n"
        (self.repo / "d.py").write_text(v2)
        self._hook("cc", "edit", {"session_id": "B", "cwd": cwd, "tool_name": "Write",
                                  "tool_input": {"file_path": str(self.repo / "d.py"), "content": v2}})
        self._kaula("drain")
        self._git("add", "d.py")
        self._git("commit", "-q", "-m", "guards")
        self._kaula("hook", "git", "post-commit")

        # `return None` appears in both sessions' edits -> ambiguous.
        out = self._kaula("why", "d.py:2", "--json")
        data = json.loads(out.stdout.decode())
        self.assertEqual(data["confidence"], "ambiguous")
        self.assertIsNotNone(data["prompt"])
        self.assertTrue(data["other_sessions"])  # the losing candidate is listed

    def test_redaction_before_write(self):
        sid = "sess-secret"
        cwd = str(self.repo)
        self._hook("cc", "session-start", {"session_id": sid, "cwd": cwd})
        self._hook("cc", "prompt", {
            "session_id": sid, "cwd": cwd,
            "prompt": "deploy with api_key = 'AKIAIOSFODNN7EXAMPLE' please",
        })
        self._kaula("drain")
        s = self._kaula("search", "deploy", "--json")
        rows = json.loads(s.stdout.decode())
        joined = json.dumps(rows)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", joined)
        self.assertIn("REDACTED", joined)


if __name__ == "__main__":
    unittest.main()
