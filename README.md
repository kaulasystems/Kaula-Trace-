# Kaula Trace — `kaula why`

**Which prompt produced this line?**

Local-first prompt↔code provenance for a single developer. Kaula watches your
Claude Code and Cursor sessions, records every prompt and every agent edit, binds
them to the commits they land in, and answers one question:

```
$ kaula why src/auth.ts:42

  src/auth.ts:42  ·  confidence: exact

  "make the token refresh handle clock skew"
    2026-08-14 09:12  ·  claude-code  ·  claude-opus-5
    session 8f3a12   ·  turn 3/7

  edit      src/auth.ts  (Write)
  commit    a91c4f "fix: tolerate clock skew in refresh"
  also edited spec/tokens.md, src/clock.ts
```

Everything stays on your machine: one SQLite file at `~/.kaula/db.sqlite`, no
server, no auth, no network. Zero third-party dependencies — just Python 3.9+.

---

## Install

```sh
pip install .            # or: pipx install .
cd your-repo
kaula init               # installs git hooks + Claude Code / Cursor config
```

`kaula init` is idempotent and non-destructive — it merges into existing
settings and fences its own git-hook lines with markers. Use `--project` to wire
Claude Code via `./.claude/settings.json` instead of `~/.claude/settings.json`.

Open a new agent session and start working. Kaula captures automatically.

## Commands

| Command | What it does |
|---|---|
| `kaula why <path>:<line>` | The primary question — prompt behind a line, with a confidence level |
| `kaula why <sha>` | Every session behind a commit |
| `kaula search <query>` | Full-text search over your prompts |
| `kaula sessions [--since 7d]` | Recent sessions and the files they touched |
| `kaula doctor` | Detect double-registered hooks and stale config |
| `kaula purge --before <date>` | Delete prompt/edit **text**, keep the hashes |
| `kaula init` | Install hooks and harness config |

`kaula why` takes `--json` for machine use and `--verbose` for the full session
transcript.

## How it works

1. **Capture (hooks).** On every prompt and every `Edit`/`Write`/`MultiEdit`,
   a hook redacts secrets, hashes the content, and appends the event to a spool.
   Hooks never touch SQLite inline and always exit 0 — they must never block or
   slow the agent. A background drain folds the spool into the database.
2. **Bind (git).** A `post-commit` hook records each commit's SHA **and** its
   stable `git patch-id`, then links the sessions whose edits landed in it.
   `post-rewrite` carries those links across rebase and amend.
3. **Attribute (`kaula why`).** `git blame` finds the commit for a line; the
   commit points at candidate sessions; the line's **content hash** points at
   the exact agent edit, and the edit points at the prompt in effect.

Attribution matches **content, not line numbers** — line numbers recorded at
edit time are worthless once the file above them shifts, but content hashes
survive. Normalisation strips trailing whitespace and drops blank lines; it does
not lowercase, and it keeps indentation.

### Confidence levels

| Level | Meaning |
|---|---|
| `exact` | The line's content traces to one specific agent edit |
| `ambiguous` | A duplicated line (e.g. `}`); resolved by surrounding context, never promoted to `exact` |
| `commit` | The commit came from a session, but this exact line was likely edited by a human afterwards or reformatted |
| `uncommitted` | Matched against an agent edit not yet committed |
| `human` | The commit isn't linked to any Kaula session |
| `unattributed` | The line predates the Kaula install, or can't be blamed |

**Kaula never reports `exact` on a heuristic.** A provenance tool that
confidently misattributes is worse than one that says "I don't know."

## Privacy

- Secrets are redacted **before** anything is written (AWS/GitHub/JWT/private-key
  shapes, `sk-`/`xox-` prefixes, and `secret`/`token`/`password`/`api_key`
  assignments). Extend the rules in `~/.kaula/redact.toml`.
- Content hashes are computed over the **pre-redaction** text, so tightening the
  rules later never breaks existing attribution.
- `kaula purge --before <date>` deletes stored prompt and edit text while keeping
  the hashes — old lines still attribute, but the words are gone.

## Development

```sh
python -m unittest discover -s tests -v
```

The test suite drives the real CLI end to end — hook → spool → drain → bind →
`why` — against throwaway git repos, and covers redaction, purge, and the
ambiguous-line disambiguation path.

## Scope (v0)

Local only. Out of scope, by design: cloud sync, auth, multi-user, dashboards,
cost tracking, hash chains, policy enforcement, forge integration.

## Licence

MIT.
