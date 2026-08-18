# `kaula why` — v0 technical spec

Local-first prompt↔code provenance for a single developer.
One question: **which prompt produced this line?**

Status: implementation spec, v0
Scope: local only, SQLite, no server, no auth, no chain.

---

## 1. Data model

Single SQLite file at `~/.kaula/db.sqlite` (override: `KAULA_DB`).
WAL mode on — hooks write concurrently with the CLI reading.

```sql
CREATE TABLE sessions (
  session_id     TEXT PRIMARY KEY,   -- from harness
  harness        TEXT NOT NULL,      -- 'claude-code' | 'cursor'
  harness_version TEXT,
  model          TEXT,
  repo_root      TEXT NOT NULL,      -- absolute path at capture time
  started_at     INTEGER NOT NULL,   -- unix ms
  ended_at       INTEGER
);

CREATE TABLE prompts (
  prompt_id   INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(session_id),
  seq         INTEGER NOT NULL,      -- turn index within session
  text        TEXT,                  -- post-redaction; NULL if redacted out
  text_hash   TEXT NOT NULL,         -- sha256 of pre-redaction text
  created_at  INTEGER NOT NULL,
  UNIQUE(session_id, seq)
);

CREATE TABLE edits (
  edit_id      INTEGER PRIMARY KEY,
  session_id   TEXT NOT NULL REFERENCES sessions(session_id),
  prompt_id    INTEGER REFERENCES prompts(prompt_id),  -- turn in effect
  tool         TEXT NOT NULL,        -- 'Edit' | 'Write' | 'MultiEdit' | ...
  path         TEXT NOT NULL,        -- repo-relative, forward slashes
  new_text     TEXT,                 -- inserted content, post-redaction
  new_hash     TEXT NOT NULL,        -- sha256 of inserted content
  line_hashes  TEXT NOT NULL,        -- JSON array of per-line sha256 (normalised)
  created_at   INTEGER NOT NULL
);
CREATE INDEX idx_edits_path ON edits(path);
CREATE INDEX idx_edits_session ON edits(session_id);

CREATE TABLE commits (
  sha         TEXT PRIMARY KEY,
  patch_id    TEXT NOT NULL,         -- git patch-id: survives rebase/amend/squash
  repo_root   TEXT NOT NULL,
  committed_at INTEGER NOT NULL
);
CREATE INDEX idx_commits_patch ON commits(patch_id);

CREATE TABLE commit_sessions (
  sha        TEXT NOT NULL REFERENCES commits(sha),
  session_id TEXT NOT NULL REFERENCES sessions(session_id),
  PRIMARY KEY (sha, session_id)
);
```

**Why `line_hashes`.** Line numbers recorded at edit time are worthless later —
everything above shifts. Content hashes survive. Attribution matches *content*,
not position. Normalise before hashing: strip trailing whitespace, drop
lines that are empty or whitespace-only, do **not** lowercase.

**Why `patch_id`.** `git patch-id` is stable across rebase, amend, cherry-pick
and squash for an identical diff. The SHA is not. Store both; resolve by SHA
first, fall back to patch-id.

---

## 2. Capture

### 2.1 Claude Code

`~/.claude/settings.json` (or project `.claude/settings.json`):

```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "kaula hook cc session-start" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "kaula hook cc prompt" }] }],
    "PostToolUse":      [{ "matcher": "Edit|Write|MultiEdit",
                           "hooks": [{ "type": "command", "command": "kaula hook cc edit" }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "kaula hook cc session-end" }] }]
  }
}
```

Payload arrives as JSON on stdin. Fields consumed: `session_id`, `cwd`,
`hook_event_name`, and for tool events `tool_name` / `tool_input`. Model and
harness version come from the session-start payload where available.

> Verify exact field names against the current hooks reference before
> implementing — the event set has changed repeatedly.

### 2.2 Cursor

`.cursor/hooks.json` (repo-level, so cloud agents are covered too):

```json
{
  "version": 1,
  "hooks": {
    "sessionStart":     [{ "command": "kaula hook cursor session-start" }],
    "beforeSubmitPrompt":[{ "command": "kaula hook cursor prompt" }],
    "afterFileEdit":    [{ "command": "kaula hook cursor edit" }],
    "sessionEnd":       [{ "command": "kaula hook cursor session-end" }]
  }
}
```

Cursor supplies `conversation_id`, `generation_id`, `model` and
`workspace_roots`. Map `conversation_id` → `session_id`.

### 2.3 Hard requirements for every hook invocation

| Requirement | Reason |
|---|---|
| Exit 0 always | Non-zero can block the agent. Never block. |
| p99 < 50ms | Hooks are in the interactive path |
| No network | Local only |
| Redact before write | Secrets land in prompts constantly |
| Idempotent on `(session_id, seq)` | Plugin + repo config can double-register |

Implementation: append the raw event to a spool file, return immediately, and
process the spool in a background worker. Never do SQLite writes inline.

### 2.4 Commit binding

`post-commit` hook (installed by `kaula init`):

```sh
kaula hook git post-commit
```

Computes `git rev-parse HEAD` and `git diff-tree -p HEAD | git patch-id --stable`,
inserts into `commits`, then links sessions:

> A session is linked to a commit if any of its `edits` touch a path in the
> commit's changed-file set **and** the edit timestamp falls between the
> previous commit's time and this commit's time.

This over-links slightly (a session editing files across two commits links to
both). That's correct — resolution happens at attribution time by content.

Also install `post-rewrite` to re-resolve SHAs after rebase/amend.

---

## 3. The attribution algorithm

This is the only hard part. Input: `path:line` at some revision (default
`HEAD`). Output: a prompt, with a confidence level.

### Stage 1 — line → commit

```
git blame -L <n>,<n> --porcelain -- <path>
```

Yields the commit that last modified that line, plus the original line content.
If the file is dirty, blame the working tree with `--contents -` and fall
through to Stage 3 against uncommitted edits.

### Stage 2 — commit → candidate sessions

Look up `sha` in `commits`. On miss (history was rewritten), compute the
commit's `patch_id` and look up by that instead. Then read `commit_sessions`.

- 0 candidates → `unattributed`
- 1 candidate → carry forward, confidence `commit`
- N candidates → carry forward all, resolve in Stage 3

### Stage 3 — commit → specific edit → prompt

Take the blamed line's content, normalise it, hash it, and search
`edits.line_hashes` for that hash, restricted to candidate sessions and to
edits whose `path` matches.

| Outcome | Confidence | Meaning |
|---|---|---|
| Exactly one edit contains the hash | `exact` | Line content traced to a specific agent edit |
| Multiple edits contain it | `ambiguous` | Duplicated line (e.g. `}`); fall back to Stage 4 |
| No edit contains it | `commit` or `human` | See below |

If Stage 2 gave exactly one session but no line matched, report confidence
`commit`: the commit came from that session, but this specific line was likely
edited by a human afterwards or reformatted. Say so in the output — do not
silently claim the prompt authored it.

If Stage 2 gave no session at all, report `human`.

### Stage 4 — disambiguation

For `ambiguous` results, score each candidate edit by context overlap: hash the
three lines above and below the target line in the current file, and count
matches against the candidate edit's `line_hashes`. Highest overlap wins; ties
resolve to the most recent edit. Confidence stays `ambiguous` — never promote to
`exact` on a heuristic.

### Stage 5 — edit → prompt

`edits.prompt_id` gives the turn in effect when the edit was made. Return that
prompt plus its session metadata.

### Failure modes to handle explicitly

| Case | Behaviour |
|---|---|
| Line predates Kaula install | `unattributed`, state the install date |
| Squashed commits | Resolve via `patch_id`; may yield multiple sessions → Stage 3 |
| Formatter rewrote the line | Content hash misses → confidence degrades to `commit` |
| Agent edit never committed | Match against edits with no commit link; label `uncommitted` |
| Two sessions edited the same line | Report the most recent, list the others |

**Never report `exact` on a heuristic.** A provenance tool that confidently
misattributes is worse than one that says "I don't know."

---

## 4. CLI surface

```
kaula init                     # install git hooks, write harness config
kaula hook <harness> <event>   # internal; reads stdin
kaula why <path>:<line>        # primary command
kaula why <sha>                # all sessions behind a commit
kaula search <query>           # FTS over prompt text
kaula sessions [--since 7d]    # recent sessions and what they touched
kaula doctor                   # detect double-registered hooks, stale config
kaula purge --before <date>    # delete prompt text, keep hashes
```

### Output

```
$ kaula why src/auth.ts:42

  src/auth.ts:42  ·  confidence: exact

  "make the token refresh handle clock skew"
    2026-08-14 09:12  ·  claude-code  ·  claude-opus-5
    session 8f3a12   ·  turn 3/7

  edit      src/auth.ts  (+14 −3)
  commit    a91c4f "fix: tolerate clock skew in refresh"
  also read spec/tokens.md, src/clock.ts
```

`--json` for machine use. `--verbose` prints the full session transcript.

---

## 5. Redaction

Runs before any write. Rules file at `~/.kaula/redact.toml`, defaults:

- High-entropy strings ≥ 20 chars matching common key shapes (AWS, GitHub PAT,
  JWT, private-key headers, `sk-`-style prefixes)
- Values assigned to identifiers matching `(?i)(secret|token|password|api[_-]?key)`
- Anything matching a user-supplied deny-list regex

Redacted spans are replaced with `[REDACTED:<kind>]`. `text_hash` is always
computed over the **pre-redaction** text so hashes stay stable if rules change.

---

## 6. Build order

1. SQLite schema + spool writer + `kaula hook` for Claude Code
2. `post-commit` binding with patch-id
3. Stage 1–3 of attribution + `kaula why`
4. Cursor hooks
5. `search`, `sessions`, `doctor`, `purge`
6. Stage 4 disambiguation
7. README with a 15-second GIF of `kaula why`

### Deliberately out of scope for v0

Cloud sync, auth, multi-user, dashboards, charts, token/cost tracking, the hash
chain, policy enforcement, forge integration. Keep the `chain` fields out of the
schema entirely — adding them later is a migration, carrying them now is dead
weight.

### Licence

MIT or Apache-2.0 on collector and CLI. The point is adoption and possibly a
de-facto capture format; a copyleft licence works against both.

---

## 7. Honest risks

1. **Formatters and linters degrade attribution silently.** Prettier on save
   will break content hashes for a meaningful share of lines. Measure this on a
   real repo before publishing accuracy claims.
2. **Harness field names drift.** Both hook APIs have changed repeatedly. Pin a
   compatibility layer and a `doctor` check that fails loudly on unknown schema.
3. **This could ship natively.** Cursor or Anthropic can add reverse-blame at
   any time. Treat this as a distribution and learning play, not a business.
