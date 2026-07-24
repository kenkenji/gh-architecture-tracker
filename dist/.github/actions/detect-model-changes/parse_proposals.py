#!/usr/bin/env python3
"""Model Change Proposalsコメントからチェック済み提案を抽出する。"""

import json
import os
import re
import sys

MARKER = "## \U0001f504 Model Change Proposals"
CHECKBOX_PATTERN = re.compile(r"^- \[([ xX])\] ")
PROPOSAL_PATTERN = re.compile(r"<!--\s*proposal:\s*(\{.*?\})\s*-->")
ALLOWED_AUTHORS = {"github-actions[bot]"}


def is_proposal_comment(body):
    return MARKER in body


def extract_approved_proposals(body):
    lines = body.splitlines()
    approved = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = CHECKBOX_PATTERN.match(line)
        if m and m.group(1).lower() == "x":
            for j in range(i + 1, min(i + 3, len(lines))):
                pm = PROPOSAL_PATTERN.search(lines[j])
                if pm:
                    try:
                        proposal = json.loads(pm.group(1))
                        approved.append(proposal)
                    except json.JSONDecodeError:
                        print(f"Warning: failed to parse proposal JSON at line {j + 1}",
                              file=sys.stderr)
                    break
        i += 1
    return approved


def main():
    body = os.environ.get("COMMENT_BODY", "")
    comment_author = os.environ.get("COMMENT_AUTHOR", "")
    app_slug = os.environ.get("APP_SLUG", "")

    if not is_proposal_comment(body):
        print("not-proposal-comment")
        sys.exit(0)

    allowed = set(ALLOWED_AUTHORS)
    if app_slug:
        allowed.add(f"{app_slug}[bot]")

    if comment_author and comment_author not in allowed:
        print(json.dumps({
            "error": "unauthorized_author",
            "author": comment_author,
        }))
        sys.exit(0)

    approved = extract_approved_proposals(body)
    result = {
        "approved_proposals": approved,
        "count": len(approved),
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
