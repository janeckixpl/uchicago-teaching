#!/usr/bin/env python3
"""
grade_submissions.py — Grade student PDF submissions against a rubric using Claude.

Usage:
    python scripts/grade_submissions.py \\
        --rubric rubric.json \\
        --submissions path/to/pdfs/ \\
        --output grades.csv

    # Dry-run (no API calls, fake scores)
    python scripts/grade_submissions.py --rubric rubric.json --submissions pdfs/ --dry-run

    # Resume a previous run (skip filenames already in grades.csv)
    python scripts/grade_submissions.py --rubric rubric.json --submissions pdfs/ --resume

Rubric JSON format:
    {
        "assignment": "Assignment 1",
        "questions": [
            {"id": "q1", "name": "Data cleaning",    "max_points": 30, "criteria": "Full marks for..."},
            {"id": "q2", "name": "Model selection",  "max_points": 40, "criteria": "..."},
            {"id": "q3", "name": "Interpretation",   "max_points": 30, "criteria": "..."}
        ]
    }

Output CSV columns (one row per student):
    filename, student_name,
    q1_score, q1_max, q1_justification,
    q2_score, q2_max, q2_justification, ...,
    total_score, total_max, pct, overall_justification

A summary file is also written alongside the CSV (<stem>_summary.txt).

NOTE: This script currently uses the direct Anthropic API. Switch to Bedrock
      (boto3 bedrock-runtime converse) before processing live student text in
      production — see CLAUDE.md project rule.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

try:
    import anthropic
except ImportError:
    sys.exit("anthropic package not found — run: pip install anthropic")


# ── Rubric ────────────────────────────────────────────────────────────────────

def load_rubric(path: str) -> dict:
    with open(path) as f:
        rubric = json.load(f)
    required = {"assignment", "questions"}
    missing = required - rubric.keys()
    if missing:
        sys.exit(f"Rubric missing keys: {missing}")
    for q in rubric["questions"]:
        for k in ("id", "name", "max_points", "criteria"):
            if k not in q:
                sys.exit(f"Question missing '{k}': {q}")
    return rubric


# ── Student name from filename ────────────────────────────────────────────────

def student_name_from_filename(filename: str) -> str:
    """
    Canvas submissions are usually named: lastname_firstname_COURSE_ASSIGNMENT_LATE_submissionid.pdf
    We take the first two underscore-separated parts and swap them → "Firstname Lastname".
    Falls back to the bare stem if the pattern doesn't match.
    """
    stem = Path(filename).stem
    parts = stem.split("_")
    if len(parts) >= 2 and all(re.match(r"^[A-Za-z\-']+$", p) for p in parts[:2]):
        last, first = parts[0], parts[1]
        return f"{first.capitalize()} {last.capitalize()}"
    return stem


# ── Grading prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a fair, consistent, and concise TA grader for a graduate data science course. \
You grade submitted work according to the rubric provided and return ONLY valid JSON — \
no markdown fences, no prose outside the JSON object. \
If a question's answer is missing or blank, award 0 points and note "Not attempted" \
as the justification. Keep each justification to 1–2 sentences."""

def build_user_prompt(rubric: dict) -> str:
    qs_text = "\n".join(
        f"  Q{i+1} ({q['id']}) — {q['name']} [{q['max_points']} pts]\n"
        f"  Criteria: {q['criteria']}"
        for i, q in enumerate(rubric["questions"])
    )
    total = sum(q["max_points"] for q in rubric["questions"])
    ids = [q["id"] for q in rubric["questions"]]
    example = {
        "student_name": "Firstname Lastname  (or best guess from the document)",
        "scores": {qid: {"score": 0, "justification": "..."} for qid in ids},
        "overall_justification": "One sentence summary.",
    }
    return f"""\
Assignment: {rubric['assignment']}
Total points: {total}

Rubric:
{qs_text}

Grade the attached submission. Return ONLY a JSON object with this exact shape:
{json.dumps(example, indent=2)}

Rules:
- "score" must be an integer between 0 and the question's max_points (inclusive).
- Do not invent partial-credit steps not implied by the rubric.
- "student_name": prefer the name from the document cover page or header; \
if absent, write "(unknown)".
"""


# ── API call ──────────────────────────────────────────────────────────────────

def grade_pdf(
    client: anthropic.Anthropic,
    pdf_path: Path,
    rubric: dict,
    model: str,
    retries: int = 3,
) -> dict[str, Any]:
    with open(pdf_path, "rb") as f:
        pdf_b64 = base64.standard_b64encode(f.read()).decode()

    prompt = build_user_prompt(rubric)
    q_ids = [q["id"] for q in rubric["questions"]]
    q_max = {q["id"]: q["max_points"] for q in rubric["questions"]}

    for attempt in range(retries):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }],
            )
            raw = response.content[0].text.strip()
            # Strip accidental markdown fences
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("`").strip()
            result = json.loads(raw)
            break
        except (json.JSONDecodeError, anthropic.APIError) as e:
            if attempt == retries - 1:
                print(f"  [warn] {pdf_path.name}: failed after {retries} attempts — {e}")
                result = _error_result(q_ids, q_max, str(e))
            else:
                wait = 2 ** attempt
                print(f"  [retry {attempt+1}] {pdf_path.name}: {e} — waiting {wait}s")
                time.sleep(wait)

    return _normalise(result, pdf_path.name, rubric)


def _error_result(q_ids: list, q_max: dict, reason: str) -> dict:
    return {
        "student_name": "(error)",
        "scores": {qid: {"score": 0, "justification": f"Grading error: {reason[:80]}"} for qid in q_ids},
        "overall_justification": f"Could not grade: {reason[:120]}",
    }


def _normalise(result: dict, filename: str, rubric: dict) -> dict:
    q_ids = [q["id"] for q in rubric["questions"]]
    q_max = {q["id"]: q["max_points"] for q in rubric["questions"]}
    scores = result.get("scores", {})

    row: dict[str, Any] = {
        "filename": filename,
        "student_name": result.get("student_name", student_name_from_filename(filename)),
    }
    total = 0
    for qid in q_ids:
        entry = scores.get(qid, {})
        raw_score = entry.get("score", 0)
        # Clamp to [0, max_points]
        score = max(0, min(int(raw_score), q_max[qid]))
        row[f"{qid}_score"] = score
        row[f"{qid}_max"] = q_max[qid]
        row[f"{qid}_justification"] = entry.get("justification", "")
        total += score

    total_max = sum(q_max.values())
    row["total_score"] = total
    row["total_max"] = total_max
    row["pct"] = round(100 * total / total_max, 1) if total_max else 0
    row["overall_justification"] = result.get("overall_justification", "")
    return row


# ── Dry-run stub ──────────────────────────────────────────────────────────────

def grade_pdf_dryrun(pdf_path: Path, rubric: dict) -> dict[str, Any]:
    import random
    q_ids = [q["id"] for q in rubric["questions"]]
    q_max = {q["id"]: q["max_points"] for q in rubric["questions"]}
    fake_scores = {qid: {"score": random.randint(0, q_max[qid]), "justification": "[dry-run]"} for qid in q_ids}
    return _normalise(
        {"student_name": student_name_from_filename(pdf_path.name), "scores": fake_scores, "overall_justification": "[dry-run]"},
        pdf_path.name, rubric,
    )


# ── CSV I/O ───────────────────────────────────────────────────────────────────

def csv_fieldnames(rubric: dict) -> list[str]:
    fields = ["filename", "student_name"]
    for q in rubric["questions"]:
        fields += [f"{q['id']}_score", f"{q['id']}_max", f"{q['id']}_justification"]
    fields += ["total_score", "total_max", "pct", "overall_justification"]
    return fields


def already_graded(output: Path, rubric: dict) -> set[str]:
    if not output.exists():
        return set()
    with open(output, newline="") as f:
        return {row["filename"] for row in csv.DictReader(f)}


def append_rows(output: Path, rows: list[dict], rubric: dict) -> None:
    fields = csv_fieldnames(rubric)
    write_header = not output.exists() or output.stat().st_size == 0
    with open(output, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


# ── Summary ───────────────────────────────────────────────────────────────────

def write_summary(output_csv: Path, rubric: dict) -> None:
    if not output_csv.exists():
        return
    with open(output_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return

    pcts = [float(r["pct"]) for r in rows]
    totals = [int(r["total_score"]) for r in rows]
    total_max = int(rows[0]["total_max"])
    n = len(rows)

    def bucket(p: float) -> str:
        if p >= 90: return "A  (90–100)"
        if p >= 80: return "B  (80–89)"
        if p >= 70: return "C  (70–79)"
        if p >= 60: return "D  (60–69)"
        return            "F  (<60)  "

    from collections import Counter
    dist = Counter(bucket(p) for p in pcts)

    lines = [
        f"Grade Distribution Summary — {rubric['assignment']}",
        "=" * 50,
        f"Students graded : {n}",
        f"Points possible : {total_max}",
        "",
        f"Mean score      : {statistics.mean(totals):.1f}  ({statistics.mean(pcts):.1f}%)",
        f"Median score    : {statistics.median(totals):.1f}  ({statistics.median(pcts):.1f}%)",
        f"Std dev         : {statistics.stdev(totals):.1f}" if n > 1 else "Std dev: N/A",
        f"Min / Max       : {min(totals)} / {max(totals)}",
        "",
        "Grade distribution:",
    ]
    for label in ["A  (90–100)", "B  (80–89)", "C  (70–79)", "D  (60–69)", "F  (<60)  "]:
        count = dist.get(label, 0)
        bar = "█" * count
        lines.append(f"  {label}  {count:3d}  {bar}")

    mean_pct = statistics.mean(pcts)
    if mean_pct < 70:
        needed = 75 - mean_pct
        lines += [
            "",
            f"Curve suggestion: mean is {mean_pct:.1f}% — adding {needed:.1f} pts",
            f"would bring it to 75%. Adjust as you see fit.",
        ]
    else:
        lines += ["", "Curve: mean is ≥70% — no curve needed (your call)."]

    lines += ["", "Per-question averages:"]
    for q in rubric["questions"]:
        qid = q["id"]
        q_scores = [int(r[f"{qid}_score"]) for r in rows]
        q_max = q["max_points"]
        avg = statistics.mean(q_scores)
        lines.append(f"  {q['name']:<30} avg {avg:.1f}/{q_max}  ({100*avg/q_max:.0f}%)")

    summary_path = output_csv.with_name(output_csv.stem + "_summary.txt")
    text = "\n".join(lines) + "\n"
    summary_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"Summary written to {summary_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Grade PDF submissions with Claude")
    parser.add_argument("--rubric",      required=True, help="Path to rubric JSON")
    parser.add_argument("--submissions", required=True, help="Folder of student PDFs")
    parser.add_argument("--output",      default="grades.csv", help="Output CSV path")
    parser.add_argument("--model",       default="claude-sonnet-4-6")
    parser.add_argument("--resume",      action="store_true", help="Skip already-graded filenames")
    parser.add_argument("--dry-run",     action="store_true", help="No API calls; use random scores")
    args = parser.parse_args()

    rubric = load_rubric(args.rubric)
    sub_folder = Path(args.submissions)
    if not sub_folder.is_dir():
        sys.exit(f"Not a directory: {sub_folder}")

    pdfs = sorted(sub_folder.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDF files found in {sub_folder}")

    output = Path(args.output)

    skipped: set[str] = set()
    if args.resume:
        skipped = already_graded(output, rubric)
        if skipped:
            print(f"Resuming — skipping {len(skipped)} already-graded file(s)")

    todo = [p for p in pdfs if p.name not in skipped]
    print(f"Grading {len(todo)} of {len(pdfs)} submission(s) — assignment: {rubric['assignment']}")

    client = None
    if not args.dry_run:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            sys.exit("ANTHROPIC_API_KEY not set — export it or use --dry-run")
        client = anthropic.Anthropic(api_key=api_key)

    for i, pdf in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {pdf.name} ...", end=" ", flush=True)
        if args.dry_run:
            row = grade_pdf_dryrun(pdf, rubric)
        else:
            row = grade_pdf(client, pdf, rubric, args.model)
        append_rows(output, [row], rubric)
        print(f"{row['total_score']}/{row['total_max']} ({row['pct']}%)  — {row['student_name']}")

    print(f"\nAll done. Results in {output}")
    write_summary(output, rubric)


if __name__ == "__main__":
    main()
