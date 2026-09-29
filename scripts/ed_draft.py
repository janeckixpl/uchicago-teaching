#!/usr/bin/env python3
"""
Ed Discussion draft generator for UChicago TA toolkit.

Pipeline per run:
  1. Poll the Ed course board for unanswered questions.
  2. Classify each question by rule — skip anything touching grades, policy,
     academic integrity, or direct solution requests.
  3. Retrieve relevant passages from a local knowledge-base directory.
  4. Draft a TA reply via AWS Bedrock (student text never touches the
     direct Anthropic API per project rules).
  5. Append every draft to a JSONL review queue. Nothing is ever posted.

To swap the model backend, subclass Drafter and pass it to run().

Usage:
    python ed_draft.py --course-id 12345
    python ed_draft.py --course-id 12345 --kb-dir course/kb/ --limit 20
    python ed_draft.py --course-id 12345 --dry-run   # classify + retrieve, no Bedrock call
"""

import argparse
import json
import re
import sys
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests


ED_TOKEN_FILE = Path.home() / ".uchicago_ed_token"
ED_BASE_URL = "https://us.edstem.org/api"

DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-5-20251001"
DEFAULT_REGION = "us-east-1"
DEFAULT_KB_DIR = Path("course/kb")
DEFAULT_QUEUE = Path("scripts/review_queue.jsonl")


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
        # Token goes into headers only — never printed, never logged
        self._session.headers.update({"x-token": _load_ed_token()})

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
    Any single match triggers a skip.
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
    by unigram overlap with the question — good enough for course material that
    uses consistent terminology.
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

    def retrieve(self, query: str, top_n: int = 3) -> list:
        """Return top_n docs ranked by token overlap with query."""
        if not self._docs:
            return []
        q_tokens = set(re.findall(r"\w+", query.lower()))
        scored = []
        for doc in self._docs:
            doc_tokens = set(re.findall(r"\w+", doc["text"].lower()))
            overlap = len(q_tokens & doc_tokens)
            if overlap > 0:
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
        context_block = (
            "\n\n---\n\n".join(context_chunks)
            if context_chunks
            else "(no matching course material found)"
        )
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
    """Appends draft records to a JSONL file. Nothing is ever posted."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict) -> None:
        with self._path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
        print(f"[queued] thread {record['thread_id']} → {self._path}")


# ── HTML helper ────────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html).strip()


# ── Pipeline ───────────────────────────────────────────────────────────────────

def run(
    course_id: int,
    kb_dir: Path,
    queue_file: Path,
    limit: int,
    dry_run: bool,
    drafter: Drafter,
) -> None:
    ed = EdClient()
    kb = KnowledgeBase(kb_dir)
    queue = ReviewQueue(queue_file)

    threads = ed.unanswered_questions(course_id, limit=limit)
    print(f"[poll] {len(threads)} unanswered question(s) on course {course_id}")

    drafted = skipped = 0
    for thread in threads:
        thread_id = thread["id"]
        title = thread.get("title", "")
        body = ed.thread_body(thread_id)

        reason = classify(title, body)
        if reason:
            print(f"[skip] thread {thread_id} '{title}' — {reason}")
            skipped += 1
            continue

        docs = kb.retrieve(f"{title} {body}")
        context_chunks = [f"[{d['path']}]\n{d['text'][:800]}" for d in docs]

        if dry_run:
            sources = ", ".join(d["path"] for d in docs) or "none"
            print(f"[dry-run] thread {thread_id} '{title}' — context: {sources}")
            drafted += 1
            continue

        reply = drafter.draft(title, body, context_chunks)
        queue.append({
            "thread_id": thread_id,
            "title": title,
            "question": body,
            "draft": reply,
            "context_files": [d["path"] for d in docs],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        })
        drafted += 1

    print(f"[done] drafted={drafted} skipped={skipped} total={drafted + skipped}")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Poll Ed Discussion for unanswered questions, classify by rule, "
            "retrieve course context, and draft replies via AWS Bedrock. "
            "All drafts go to a review queue — nothing is posted automatically."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--course-id", type=int, required=True, metavar="ID")
    parser.add_argument(
        "--kb-dir",
        type=Path,
        default=DEFAULT_KB_DIR,
        metavar="DIR",
        help=f"Directory of .md/.txt/.html course materials (default: {DEFAULT_KB_DIR})",
    )
    parser.add_argument(
        "--queue-file",
        type=Path,
        default=DEFAULT_QUEUE,
        metavar="FILE",
        help=f"JSONL file to append draft records (default: {DEFAULT_QUEUE})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        metavar="N",
        help="Max questions to fetch per run (default: 50)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify and retrieve context but skip the Bedrock call",
    )
    parser.add_argument(
        "--model-id",
        default=DEFAULT_MODEL,
        metavar="MODEL",
        help=f"Bedrock model ID (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--region",
        default=DEFAULT_REGION,
        metavar="REGION",
        help=f"AWS region for Bedrock (default: {DEFAULT_REGION})",
    )

    args = parser.parse_args()
    drafter = BedrockDrafter(model_id=args.model_id, region=args.region)
    run(
        course_id=args.course_id,
        kb_dir=args.kb_dir,
        queue_file=args.queue_file,
        limit=args.limit,
        dry_run=args.dry_run,
        drafter=drafter,
    )


if __name__ == "__main__":
    main()
