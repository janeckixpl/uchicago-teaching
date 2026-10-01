#!/usr/bin/env python3
"""
Ed Discussion draft generator for UChicago TA toolkit.

Pipeline per run (poll subcommand):
  1. Poll the Ed course board for unanswered questions.
  2. Classify each by rule — skip grades, policy, integrity, direct solutions.
     Skips are written to the queue as status=skipped so the digest can count them.
  3. Retrieve relevant passages from a local knowledge-base directory.
     If no document meets --min-overlap, write status=needs_context and stop —
     no Bedrock call is made.
  4. Draft a TA reply via AWS Bedrock (student text never touches the direct
     Anthropic API per project rules).
  5. Append the draft to the JSONL review queue as status=pending.
     Nothing is ever posted automatically.

To swap the model backend, subclass Drafter and pass it to poll().

Usage:
    python ed_draft.py poll --course-id 12345
    python ed_draft.py poll --course-id 12345 --min-overlap 5 --dry-run
    python ed_draft.py digest
    python ed_draft.py digest --days 14 --queue-file scripts/review_queue.jsonl
"""

import argparse
import json
import re
import sys
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests


ED_TOKEN_FILE = Path.home() / ".uchicago_ed_token"
ED_BASE_URL = "https://us.edstem.org/api"

DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-5-20251001"
DEFAULT_REGION = "us-east-1"
DEFAULT_KB_DIR = Path("course/kb")
DEFAULT_QUEUE = Path("scripts/review_queue.jsonl")
DEFAULT_MIN_OVERLAP = 3


# ── Token ──────────────────────────────────────────────────────────────────────

def _load_ed_token() -> str:
    if not ED_TOKEN_FILE.exists():
        sys.exit(f"error: Ed token file not found: {ED_TOKEN_FILE}")
    token = ED_TOKEN_FILE.read_text().strip()
    if not token:
        sys.exit(f"error: Ed token file is empty: {ED_TOKEN_FILE}")
    return token


# ── Ed API client ──────────────────────────────────────────────────────────────

class EdClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {_load_ed_token()}",
            "User-Agent": "Mozilla/5.0",
        })

    def _get(self, path: str, params: dict = None) -> dict:
        resp = self._session.get(f"{ED_BASE_URL}{path}", params=params)
        if not resp.ok:
            sys.exit(
                f"Ed API error {resp.status_code} GET {resp.url}\n{resp.text[:400]}"
            )
        return resp.json()

    def unanswered_questions(self, course_id: int, limit: int = 50) -> list:
        """Return threads that are type=question and have no accepted answer."""
        data = self._get(
            f"/courses/{course_id}/threads",
            params={"limit": limit, "filter": "unanswered", "sort": "date"},
        )
        threads = data.get("threads", [])
        return [
            t for t in threads
            if t.get("type") == "question" and not t.get("is_answered")
        ]

    def thread_body(self, thread_id: int) -> str:
        """Fetch the full thread and return its body as plain text."""
        data = self._get(f"/threads/{thread_id}")
        raw = data.get("thread", {}).get("text", "")
        return _strip_html(raw)


# ── Classifier ─────────────────────────────────────────────────────────────────

_GRADE_TERMS = {
    "grade", "regrade", "partial credit", "points off",
    "deduct", "rubric", "score", "how many points",
}
_POLICY_TERMS = {
    "late policy", "extension", "deadline", "due date",
    "syllabus", "office hours", "absence", "excused", "makeup",
}
_INTEGRITY_TERMS = {
    "academic integrity", "plagiarism", "honor code",
    "allowed to use", "can i use", "can we use", "is it ok to use",
    "allowed to work", "collaboration policy",
}
_SOLUTION_TERMS = {
    "just give me", "tell me the answer", "what is the answer",
    "solve this for me", "write my", "do my homework",
    "give me the code", "write the code for me", "just tell me",
}


def classify(title: str, body: str) -> Optional[str]:
    """
    Return a human-readable skip reason, or None if safe to draft.

    Checked in order: grades → policy → integrity → direct solution request.
    """
    text = (title + " " + body).lower()
    if any(t in text for t in _GRADE_TERMS):
        return "grades/scores"
    if any(t in text for t in _POLICY_TERMS):
        return "course policy/logistics"
    if any(t in text for t in _INTEGRITY_TERMS):
        return "academic integrity"
    if any(t in text for t in _SOLUTION_TERMS):
        return "direct solution request"
    return None


# ── Knowledge base ─────────────────────────────────────────────────────────────

class KnowledgeBase:
    """
    Loads .md / .txt / .html files from kb_dir. Retrieves the top-N documents
    by unigram overlap with the question, subject to a minimum overlap threshold.
    """

    def __init__(self, kb_dir: Path) -> None:
        self._docs: list = []
        if not kb_dir.exists():
            print(f"[kb] directory not found: {kb_dir} — proceeding without context")
            return
        for path in sorted(kb_dir.rglob("*")):
            if path.suffix in {".md", ".txt", ".html"} and path.is_file():
                text = _strip_html(path.read_text(errors="replace"))
                self._docs.append({"path": str(path.relative_to(kb_dir)), "text": text})
        print(f"[kb] loaded {len(self._docs)} document(s) from {kb_dir}")

    def retrieve(self, query: str, top_n: int = 3, min_score: int = DEFAULT_MIN_OVERLAP) -> list:
        """
        Return top_n docs with overlap >= min_score. Returns [] when no doc
        meets the threshold — callers must treat an empty result as a hold signal.
        """
        if not self._docs:
            return []
        q_tokens = set(re.findall(r"\w+", query.lower()))
        scored = []
        for doc in self._docs:
            doc_tokens = set(re.findall(r"\w+", doc["text"].lower()))
            overlap = len(q_tokens & doc_tokens)
            if overlap >= min_score:
                scored.append((overlap, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [doc for _, doc in scored[:top_n]]


# ── Model backend (swappable) ──────────────────────────────────────────────────

class Drafter(ABC):
    """Abstract base — swap subclasses to change the model backend."""

    @abstractmethod
    def draft(self, title: str, body: str, context_chunks: list) -> str: ...


class BedrockDrafter(Drafter):
    """
    Calls Claude on AWS Bedrock via the Converse API.

    Student text stays within AWS/Bedrock per project rules — no calls to the
    direct Anthropic API here.
    """

    def __init__(self, model_id: str = DEFAULT_MODEL, region: str = DEFAULT_REGION) -> None:
        try:
            import boto3
            from botocore.exceptions import BotoCoreError, ClientError
            self._BotoCoreError = BotoCoreError
            self._ClientError = ClientError
            self._client = boto3.client("bedrock-runtime", region_name=region)
        except ImportError:
            sys.exit("error: boto3 is required — pip install boto3")
        except Exception as exc:
            sys.exit(f"error: could not create Bedrock client: {exc}")
        self._model_id = model_id

    def draft(self, title: str, body: str, context_chunks: list) -> str:
        context_block = "\n\n---\n\n".join(context_chunks)
        prompt = (
            "You are a helpful TA for a UChicago course. "
            "A student posted the question below on Ed Discussion.\n\n"
            f"Title: {title}\n\n"
            f"Question:\n{body}\n\n"
            "Relevant course material:\n"
            f"{context_block}\n\n"
            "Write a TA reply that:\n"
            "- Guides the student toward the answer without giving it away\n"
            "- Cites the course material by filename when it's directly relevant\n"
            "- Is encouraging, clear, and under 150 words unless more is needed\n\n"
            "Reply with only the text of the draft."
        )
        try:
            resp = self._client.converse(
                modelId=self._model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": 512, "temperature": 0.3},
            )
            return resp["output"]["message"]["content"][0]["text"]
        except (self._BotoCoreError, self._ClientError) as exc:
            sys.exit(f"error: Bedrock call failed: {exc}")


# ── Review queue ───────────────────────────────────────────────────────────────

class ReviewQueue:
    """Appends records to a JSONL file. Nothing is ever posted."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict) -> None:
        with self._path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")


# ── HTML helper ────────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html).strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Poll pipeline ──────────────────────────────────────────────────────────────

def poll(
    course_id: int,
    kb_dir: Path,
    queue_file: Path,
    limit: int,
    min_overlap: int,
    dry_run: bool,
    drafter: Drafter,
) -> None:
    ed = EdClient()
    kb = KnowledgeBase(kb_dir)
    queue = ReviewQueue(queue_file)

    threads = ed.unanswered_questions(course_id, limit=limit)
    print(f"[poll] {len(threads)} unanswered question(s) on course {course_id}")

    counts: Counter = Counter()
    for thread in threads:
        thread_id = thread["id"]
        title = thread.get("title", "")
        body = ed.thread_body(thread_id)

        reason = classify(title, body)
        if reason:
            print(f"[skip] thread {thread_id} '{title}' — {reason}")
            if not dry_run:
                queue.append({
                    "thread_id": thread_id,
                    "title": title,
                    "timestamp": _now(),
                    "status": "skipped",
                    "skip_reason": reason,
                })
            counts["skipped"] += 1
            continue

        docs = kb.retrieve(f"{title} {body}", min_score=min_overlap)

        if not docs:
            print(f"[hold] thread {thread_id} '{title}' — no KB match (min_overlap={min_overlap})")
            if not dry_run:
                queue.append({
                    "thread_id": thread_id,
                    "title": title,
                    "question": body,
                    "timestamp": _now(),
                    "status": "needs_context",
                })
            counts["needs_context"] += 1
            continue

        context_chunks = [f"[{d['path']}]\n{d['text'][:800]}" for d in docs]

        if dry_run:
            sources = ", ".join(d["path"] for d in docs)
            print(f"[dry-run] thread {thread_id} '{title}' — context: {sources}")
            counts["drafted"] += 1
            continue

        reply = drafter.draft(title, body, context_chunks)
        queue.append({
            "thread_id": thread_id,
            "title": title,
            "question": body,
            "draft": reply,
            "context_files": [d["path"] for d in docs],
            "timestamp": _now(),
            "status": "pending",
        })
        print(f"[queued] thread {thread_id} '{title}' → {queue_file}")
        counts["drafted"] += 1

    total = sum(counts.values())
    print(
        f"[done] drafted={counts['drafted']} "
        f"needs_context={counts['needs_context']} "
        f"skipped={counts['skipped']} "
        f"total={total}"
    )


# ── Digest ─────────────────────────────────────────────────────────────────────

def digest(queue_file: Path, days: int) -> None:
    if not queue_file.exists():
        sys.exit(f"error: queue file not found: {queue_file}")

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    records = []
    with queue_file.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = rec.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts)
                if dt < cutoff:
                    continue
            except ValueError:
                continue
            records.append(rec)

    counts: Counter = Counter(r["status"] for r in records)
    total = len(records)

    since = cutoff.strftime("%Y-%m-%d")
    until = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    width = 54
    print(f"\nDigest — last {days} day(s)  ({since} → {until})")
    print("─" * width)
    print(f"  {'drafted (pending)':<28} {counts['pending']:>4}")
    print(f"  {'held (needs_context)':<28} {counts['needs_context']:>4}")
    print(f"  {'skipped by classifier':<28} {counts['skipped']:>4}")
    print(f"  {'─' * 33}")
    print(f"  {'total':<28} {total:>4}")

    held = [r for r in records if r["status"] == "needs_context"]
    if held:
        print(f"\nQuestions needing KB coverage ({len(held)}):")
        for r in held:
            date = r.get("timestamp", "")[:10]
            print(f"  [thread {r['thread_id']}] \"{r['title']}\"  ({date})")

    skipped = [r for r in records if r["status"] == "skipped"]
    if skipped:
        print(f"\nSkipped by classifier ({len(skipped)}):")
        for r in skipped:
            date = r.get("timestamp", "")[:10]
            print(f"  [thread {r['thread_id']}] \"{r['title']}\" — {r.get('skip_reason', '?')}  ({date})")

    print()


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ed Discussion TA draft tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # poll
    p = sub.add_parser(
        "poll",
        help="Fetch unanswered questions and draft replies",
        description=(
            "Poll Ed for unanswered questions, classify by rule, retrieve course "
            "context, and draft replies via AWS Bedrock. Drafts are written to the "
            "review queue — nothing is posted. Questions with no KB match above "
            "--min-overlap are held as needs_context without calling Bedrock."
        ),
    )
    p.add_argument("--course-id", type=int, required=True, metavar="ID")
    p.add_argument(
        "--kb-dir", type=Path, default=DEFAULT_KB_DIR, metavar="DIR",
        help=f"Directory of .md/.txt/.html course materials (default: {DEFAULT_KB_DIR})",
    )
    p.add_argument(
        "--queue-file", type=Path, default=DEFAULT_QUEUE, metavar="FILE",
        help=f"JSONL file to append records (default: {DEFAULT_QUEUE})",
    )
    p.add_argument(
        "--limit", type=int, default=50, metavar="N",
        help="Max questions to fetch per run (default: 50)",
    )
    p.add_argument(
        "--min-overlap", type=int, default=DEFAULT_MIN_OVERLAP, metavar="N",
        help=(
            "Minimum KB token-overlap score required to draft a reply. "
            f"Questions below this are held as needs_context (default: {DEFAULT_MIN_OVERLAP})"
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Classify and retrieve context but skip Bedrock and queue writes",
    )
    p.add_argument(
        "--model-id", default=DEFAULT_MODEL, metavar="MODEL",
        help=f"Bedrock model ID (default: {DEFAULT_MODEL})",
    )
    p.add_argument(
        "--region", default=DEFAULT_REGION, metavar="REGION",
        help=f"AWS region for Bedrock (default: {DEFAULT_REGION})",
    )

    # digest
    d = sub.add_parser(
        "digest",
        help="Summarise the review queue for a date window",
        description=(
            "Read the review queue and print a summary of drafted, held, and "
            "skipped questions. Lists needs_context titles so you know which "
            "topics to add to the knowledge base."
        ),
    )
    d.add_argument(
        "--queue-file", type=Path, default=DEFAULT_QUEUE, metavar="FILE",
        help=f"JSONL queue file to read (default: {DEFAULT_QUEUE})",
    )
    d.add_argument(
        "--days", type=int, default=7, metavar="N",
        help="Number of days to look back (default: 7)",
    )

    args = parser.parse_args()

    if args.cmd == "poll":
        drafter = BedrockDrafter(model_id=args.model_id, region=args.region)
        poll(
            course_id=args.course_id,
            kb_dir=args.kb_dir,
            queue_file=args.queue_file,
            limit=args.limit,
            min_overlap=args.min_overlap,
            dry_run=args.dry_run,
            drafter=drafter,
        )
    elif args.cmd == "digest":
        digest(queue_file=args.queue_file, days=args.days)


if __name__ == "__main__":
    main()
