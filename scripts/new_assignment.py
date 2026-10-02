#!/usr/bin/env python3
"""
Scaffold a new assignment folder under course/<slug>/assignments/<name>/.

Usage:
    python scripts/new_assignment.py --course adsp-32030 --name a2
    python scripts/new_assignment.py --course adsp-31014 --name a2 --questions 5 --points 20

Creates:
    course/<course>/<name>/
        rubric.json   — template with N questions at P points each
        solution.md   — empty solution template
        grades/       — gitignored; grading CSV output goes here
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COURSE_SLUGS = {"adsp-32030", "adsp-31014"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Scaffold a new assignment folder")
    parser.add_argument("--course",    required=True, choices=sorted(COURSE_SLUGS),
                        help="Course slug")
    parser.add_argument("--name",      required=True,
                        help="Assignment name, e.g. a2")
    parser.add_argument("--questions", type=int, default=4,
                        help="Number of rubric questions (default: 4)")
    parser.add_argument("--points",    type=int, default=25,
                        help="Points per question (default: 25)")
    args = parser.parse_args()

    base = REPO_ROOT / "course" / args.course / "assignments" / args.name

    if base.exists():
        sys.exit(f"Already exists: {base}\nDelete it first if you want to re-scaffold.")

    grades_dir = base / "grades"
    grades_dir.mkdir(parents=True)

    # rubric.json
    rubric = {
        "assignment": args.name.upper(),
        "questions": [
            {
                "id": f"q{i}",
                "name": f"Question {i}",
                "max_points": args.points,
                "criteria": "Award full marks for...",
            }
            for i in range(1, args.questions + 1)
        ],
    }
    (base / "rubric.json").write_text(json.dumps(rubric, indent=2) + "\n", encoding="utf-8")

    # solution.md
    sections = "\n\n".join(
        f"## Q{i}: Question {i}\n\n..."
        for i in range(1, args.questions + 1)
    )
    (base / "solution.md").write_text(
        f"# {args.name.upper()} Solution\n\n{sections}\n", encoding="utf-8"
    )

    total = args.questions * args.points
    print(f"Created {base.relative_to(REPO_ROOT)}/")
    print(f"  rubric.json  — {args.questions} questions × {args.points} pts = {total} pts total")
    print(f"  solution.md  — fill in model answers")
    print(f"  grades/      — gitignored; grading CSV output goes here")
    print()
    print("Edit rubric.json, then grade with:")
    print(f"  python scripts/grade_submissions.py \\")
    print(f"    --rubric course/{args.course}/assignments/{args.name}/rubric.json \\")
    print(f"    --submissions ~/Downloads/{args.name}_submissions/ \\")
    print(f"    --output course/{args.course}/assignments/{args.name}/grades/{args.name}_grades.csv")


if __name__ == "__main__":
    main()
