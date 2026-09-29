#!/usr/bin/env python3
"""
Idempotent Canvas content publisher for UChicago TA toolkit.

Find-then-update pattern: searches for existing content by title/name before
creating, so re-running the same command is always safe. All content is created
as drafts (unpublished) unless --publish is passed explicitly.

After every POST, a GET verification confirms the object exists on Canvas.

Usage:
    python publish_to_canvas.py page         --course-id 12345 --title "..." --body "..."
    python publish_to_canvas.py announcement --course-id 12345 --title "..." --message "..."
    python publish_to_canvas.py assignment   --course-id 12345 --name "..."  --description "..."

Prefix any content argument with '@' to read from a file:
    --body @course/week1/notes.html

Add --publish to go live immediately (requires explicit intent).
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

import requests


TOKEN_FILE = Path.home() / ".uchicago_canvas_token"
BASE_URL = "https://canvas.uchicago.edu/api/v1"


# ── Token ──────────────────────────────────────────────────────────────────────

def _load_token() -> str:
    if not TOKEN_FILE.exists():
        sys.exit(f"error: token file not found: {TOKEN_FILE}")
    token = TOKEN_FILE.read_text().strip()
    if not token:
        sys.exit(f"error: token file is empty: {TOKEN_FILE}")
    return token


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _raise_for_status(resp: requests.Response) -> None:
    if not resp.ok:
        # Surface method + URL + body without ever touching the token
        sys.exit(
            f"Canvas API error {resp.status_code} "
            f"{resp.request.method} {resp.url}\n{resp.text[:600]}"
        )


def _next_link(header: str) -> Optional[str]:
    """Parse the RFC 5988 Link header and return the 'next' URL, or None."""
    for part in header.split(","):
        part = part.strip()
        if 'rel="next"' in part:
            return part.split(";")[0].strip().lstrip("<").rstrip(">")
    return None


# ── Canvas client ──────────────────────────────────────────────────────────────

class CanvasClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {_load_token()}"})

    # -- low-level -------------------------------------------------------------

    def _get(self, path: str, params: dict = None) -> dict:
        resp = self._session.get(f"{BASE_URL}{path}", params=params)
        _raise_for_status(resp)
        return resp.json()

    def _get_all(self, path: str, params: dict = None) -> list:
        """Exhaust all pages of a paginated Canvas response via Link headers."""
        merged = {**(params or {}), "per_page": 100}
        items: list = []
        url = f"{BASE_URL}{path}"
        while url:
            resp = self._session.get(url, params=merged)
            _raise_for_status(resp)
            items.extend(resp.json())
            url = _next_link(resp.headers.get("Link", ""))
            merged = {}  # subsequent pages carry params in the URL itself
        return items

    def _post(self, path: str, data: dict) -> dict:
        resp = self._session.post(f"{BASE_URL}{path}", json=data)
        _raise_for_status(resp)
        return resp.json()

    def _put(self, path: str, data: dict) -> dict:
        resp = self._session.put(f"{BASE_URL}{path}", json=data)
        _raise_for_status(resp)
        return resp.json()

    # -- find helper -----------------------------------------------------------

    @staticmethod
    def _find(items: list, key: str, value: str) -> Optional[dict]:
        needle = value.strip().lower()
        for item in items:
            if item.get(key, "").strip().lower() == needle:
                return item
        return None

    # ── Pages ─────────────────────────────────────────────────────────────────

    def upsert_page(
        self,
        course_id: int,
        title: str,
        body: str,
        published: bool = False,
    ) -> dict:
        existing = self._find(
            self._get_all(f"/courses/{course_id}/pages"), "title", title
        )

        if existing:
            result = self._put(
                f"/courses/{course_id}/pages/{existing['url']}",
                {"wiki_page": {"title": title, "body": body, "published": published}},
            )
            _report("page", "updated", result["title"], result.get("published"))
        else:
            result = self._post(
                f"/courses/{course_id}/pages",
                {"wiki_page": {"title": title, "body": body, "published": published}},
            )
            # Verify the object actually exists
            verify = self._get(f"/courses/{course_id}/pages/{result['url']}")
            if verify["url"] != result["url"]:
                sys.exit("error: page GET verification failed after create")
            _report("page", "created", result["title"], result.get("published"))

        return result

    # ── Announcements ──────────────────────────────────────────────────────────

    def upsert_announcement(
        self,
        course_id: int,
        title: str,
        message: str,
        published: bool = False,
    ) -> dict:
        existing = self._find(
            self._get_all(
                f"/courses/{course_id}/discussion_topics",
                {"only_announcements": "true"},
            ),
            "title",
            title,
        )

        if existing:
            result = self._put(
                f"/courses/{course_id}/discussion_topics/{existing['id']}",
                {"title": title, "message": message, "published": published},
            )
            _report("announcement", "updated", result["title"], result.get("published"))
        else:
            result = self._post(
                f"/courses/{course_id}/discussion_topics",
                {
                    "title": title,
                    "message": message,
                    "is_announcement": True,
                    "published": published,
                },
            )
            verify = self._get(
                f"/courses/{course_id}/discussion_topics/{result['id']}"
            )
            if verify["id"] != result["id"]:
                sys.exit("error: announcement GET verification failed after create")
            _report("announcement", "created", result["title"], result.get("published"))

        return result

    # ── Assignments ────────────────────────────────────────────────────────────

    def upsert_assignment(
        self,
        course_id: int,
        name: str,
        description: str,
        points_possible: float = 0.0,
        due_at: Optional[str] = None,
        submission_types: Optional[list] = None,
        published: bool = False,
    ) -> dict:
        payload: dict = {
            "assignment": {
                "name": name,
                "description": description,
                "points_possible": points_possible,
                "submission_types": submission_types or ["online_upload"],
                "published": published,
            }
        }
        if due_at:
            payload["assignment"]["due_at"] = due_at

        existing = self._find(
            self._get_all(f"/courses/{course_id}/assignments"), "name", name
        )

        if existing:
            result = self._put(
                f"/courses/{course_id}/assignments/{existing['id']}", payload
            )
            _report("assignment", "updated", result["name"], result.get("published"))
        else:
            result = self._post(f"/courses/{course_id}/assignments", payload)
            verify = self._get(f"/courses/{course_id}/assignments/{result['id']}")
            if verify["id"] != result["id"]:
                sys.exit("error: assignment GET verification failed after create")
            _report("assignment", "created", result["name"], result.get("published"))

        return result


# ── Output ─────────────────────────────────────────────────────────────────────

def _report(kind: str, action: str, name: str, published: Optional[bool]) -> None:
    status = "published" if published else "draft"
    print(f"[{action}] {kind}: '{name}' ({status})")


# ── Content loading ────────────────────────────────────────────────────────────

def _read_content(value: str) -> str:
    """Return value as-is, or read from file when the argument starts with '@'."""
    if value.startswith("@"):
        path = Path(value[1:])
        if not path.exists():
            sys.exit(f"error: content file not found: {path}")
        return path.read_text()
    return value


# ── CLI ────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Idempotent Canvas publisher. "
            "Finds existing content by title/name and updates it; "
            "creates only when nothing matches. "
            "All content stays unpublished until --publish is passed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Publish immediately. Omit to keep as draft (default).",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    # page
    p = sub.add_parser("page", help="Create or update a Canvas wiki page")
    p.add_argument("--course-id", type=int, required=True, metavar="ID")
    p.add_argument("--title", required=True)
    body_group = p.add_mutually_exclusive_group(required=True)
    body_group.add_argument(
        "--body",
        metavar="HTML|@FILE",
        help="HTML body text, or @path/to/file.html to read from disk",
    )
    body_group.add_argument(
        "--body-file",
        dest="body_file",
        metavar="FILE",
        help="Path to an HTML file (shorthand for --body @FILE)",
    )

    # announcement
    a = sub.add_parser("announcement", help="Create or update an announcement")
    a.add_argument("--course-id", type=int, required=True, metavar="ID")
    a.add_argument("--title", required=True)
    a.add_argument(
        "--message",
        required=True,
        metavar="HTML|@FILE",
        help="HTML message body, or @path/to/file.html",
    )

    # assignment
    s = sub.add_parser("assignment", help="Create or update an assignment")
    s.add_argument("--course-id", type=int, required=True, metavar="ID")
    s.add_argument("--name", required=True)
    s.add_argument(
        "--description",
        required=True,
        metavar="HTML|@FILE",
        help="HTML description, or @path/to/file.html",
    )
    s.add_argument("--points", type=float, default=0.0, dest="points_possible")
    s.add_argument(
        "--due-at",
        metavar="ISO8601",
        help="Due date in ISO 8601, e.g. 2026-10-15T23:59:00-05:00",
    )
    s.add_argument(
        "--submission-types",
        nargs="+",
        default=["online_upload"],
        metavar="TYPE",
        help=(
            "Space-separated submission type(s). "
            "Common values: online_upload online_text_entry none "
            "(default: online_upload)"
        ),
    )

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    client = CanvasClient()

    if args.cmd == "page":
        body_arg = f"@{args.body_file}" if args.body_file else args.body
        client.upsert_page(
            course_id=args.course_id,
            title=args.title,
            body=_read_content(body_arg),
            published=args.publish,
        )
    elif args.cmd == "announcement":
        client.upsert_announcement(
            course_id=args.course_id,
            title=args.title,
            message=_read_content(args.message),
            published=args.publish,
        )
    elif args.cmd == "assignment":
        client.upsert_assignment(
            course_id=args.course_id,
            name=args.name,
            description=_read_content(args.description),
            points_possible=args.points_possible,
            due_at=args.due_at,
            submission_types=args.submission_types,
            published=args.publish,
        )


if __name__ == "__main__":
    main()
