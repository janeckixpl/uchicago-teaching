# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Context

Jan Mrozowski — TA for ADSP 31014 (two sections) and co-teacher for ADSP 32030, Autumn 2026, University of Chicago MS in Applied Data Science.

## Credentials

- Canvas API token: `~/.uchicago_canvas_token` — never print or commit
- Canvas base URL: `https://canvas.uchicago.edu/api/v1`
- Ed Discussion token: `~/.uchicago_ed_token` — never print or commit
- Ed base URL: `https://us.edstem.org/api`

## Autumn 2026 Course IDs

| Course | Canvas | Ed |
|--------|--------|----|
| ADSP 31014 IP03 (TA) | 73389 | 107568 |
| ADSP 31014 IP11 (TA) | 73394 | 107579 |
| ADSP 32030 IP01 (co-teacher) | 73379 | 107449 |

## Rules

- All Canvas content stays **unpublished** until Jan explicitly approves — except announcements, which Canvas requires to be published on creation.
- Student text goes through **AWS Bedrock only** — never the direct Anthropic API.
- After every Canvas POST, do a GET to verify the object exists.
- macOS sandbox blocks Bash from reading `~/Desktop` and `~/Downloads`; copy files to `/tmp` first via `mcp__terminal__run_in_terminal`.

## Scripts

### `scripts/publish_to_canvas.py`

Idempotent Canvas publisher. Find-then-update: searches for existing content by title/name before creating, so re-running is always safe.

```bash
# Create/update a wiki page (draft by default)
python scripts/publish_to_canvas.py page --course-id 73379 --title "Week 1 Notes" --body @course/week1/notes.html

# Create/update an announcement (always publishes — Canvas limitation)
python scripts/publish_to_canvas.py announcement --course-id 73379 --title "..." --message "..."

# Create/update an assignment
python scripts/publish_to_canvas.py assignment --course-id 73379 --name "A1" --description @a1.html --points 100 --due-at 2026-10-15T23:59:00-05:00

# Add --publish to go live (pages/assignments only — not needed for announcements)
python scripts/publish_to_canvas.py page ... --publish
```

Canvas file upload uses a two-step API (not covered by this script): POST `/courses/:id/files` to get an upload URL, then POST the file to that URL. The S3 response is 201 on success (not 200).

### `scripts/ed_draft.py`

Polls Ed Discussion for unanswered questions, classifies by rule (grades/policy/integrity/solution requests are skipped), retrieves course KB context, and drafts replies via Bedrock. **Nothing is ever posted automatically** — drafts go to a JSONL review queue.

```bash
# Poll for unanswered questions and queue drafts
python scripts/ed_draft.py poll --course-id 107449

# Dry run (no Bedrock calls, no queue writes)
python scripts/ed_draft.py poll --course-id 107449 --dry-run

# Review queue digest for the last 7 days
python scripts/ed_draft.py digest

# Digest for a longer window
python scripts/ed_draft.py digest --days 14
```

KB documents go in `course/kb/` as `.md`, `.txt`, or `.html` files. Questions with no KB match above `--min-overlap` (default 3) are held as `needs_context` — the digest lists them so you know what to add.

## Architecture

```
scripts/
  publish_to_canvas.py   — CanvasClient wraps the REST API; upsert_page / upsert_announcement / upsert_assignment
  ed_draft.py            — EdClient polls threads; KnowledgeBase retrieves context; BedrockDrafter calls Claude via Converse API; ReviewQueue appends to JSONL
course/
  kb/                    — knowledge-base files for ed_draft.py retrieval
scripts/review_queue.jsonl  — append-only JSONL of drafted/skipped/needs_context records
```

The two scripts are standalone — no shared modules, no package install needed beyond `requests` (and `boto3` for `ed_draft.py` poll).
