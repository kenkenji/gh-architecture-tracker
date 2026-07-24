#!/usr/bin/env python3
"""変更提案をチェックボックス付きPRコメントとして生成する。"""

import argparse
import json
import sys

MARKER = "## \U0001f504 Model Change Proposals"


def build_proposal_line(proposal):
    p_json = json.dumps(proposal, ensure_ascii=False, separators=(",", ":"))

    if proposal.get("target") == "relation":
        from_id = proposal.get("from", "")
        to_id = proposal.get("to", "")
        desc = proposal.get("description", "")
        tech = proposal.get("technology", "")
        tech_part = f" (technology: {tech})" if tech else ""
        reasoning = proposal.get("reasoning", "")
        line = f'- [ ] `{from_id}` → `{to_id}` — "{desc}"{tech_part}\n'
        line += f"<!-- proposal: {p_json} -->\n"
        if reasoning:
            line += f"  > \U0001f4dd {reasoning}\n"
        return line

    comp_id = proposal.get("id", "")
    action = proposal.get("action", "add")

    if action == "add":
        name = proposal.get("name", comp_id)
        desc = proposal.get("description", "")
        parent = proposal.get("parent", "")
        tech = proposal.get("technology", "")
        detail_parts = []
        if parent:
            detail_parts.append(f"parent: `{parent}`")
        if tech:
            detail_parts.append(f"technology: {tech}")
        detail = f" ({', '.join(detail_parts)})" if detail_parts else ""
        reasoning = proposal.get("reasoning", "")
        line = f"- [ ] `{comp_id}` — {desc}{detail}\n"
        line += f"<!-- proposal: {p_json} -->\n"
        if reasoning:
            line += f"  > \U0001f4dd {reasoning}\n"
        return line

    elif action == "remove":
        reasoning = proposal.get("reasoning", "")
        line = f"- [ ] `{comp_id}` — コンポーネントの削除\n"
        line += f"<!-- proposal: {p_json} -->\n"
        if reasoning:
            line += f"  > \U0001f4dd {reasoning}\n"
        return line

    elif action == "modify":
        reasoning = proposal.get("reasoning", "")
        desc = proposal.get("description", "")
        line = f"- [ ] `{comp_id}` — {desc}\n"
        line += f"<!-- proposal: {p_json} -->\n"
        if reasoning:
            line += f"  > \U0001f4dd {reasoning}\n"
        return line

    return ""


def build_comment(proposals, pr_number, detection_method):
    adds = [p for p in proposals if p.get("action") == "add" and p.get("target") != "relation"]
    modifies = [p for p in proposals if p.get("action") == "modify"]
    relation_adds = [p for p in proposals if p.get("target") == "relation"]
    removes = [p for p in proposals if p.get("action") == "remove"]

    lines = []
    lines.append(MARKER)
    lines.append("")
    lines.append("<!-- source: auto -->")
    lines.append("<!-- proposal-version: 1 -->")
    lines.append("")
    lines.append(f"**PR #{pr_number}** のマージにより、アーキテクチャモデルの更新が必要と検出されました。")
    lines.append("承認する提案のチェックボックスをオンにしてください。")
    lines.append("")

    if adds:
        lines.append("### コンポーネントの追加")
        lines.append("")
        for p in adds:
            lines.append(build_proposal_line(p))

    if modifies:
        lines.append("### コンポーネントの変更")
        lines.append("")
        for p in modifies:
            lines.append(build_proposal_line(p))

    if relation_adds:
        lines.append("### Relation（関係）の追加")
        lines.append("")
        for p in relation_adds:
            lines.append(build_proposal_line(p))

    if removes:
        lines.append("### コンポーネントの削除")
        lines.append("")
        for p in removes:
            lines.append(build_proposal_line(p))

    lines.append("---")
    method_label = "静的解析 + AI" if "llm" in detection_method else "静的解析"
    lines.append(
        f'<sub>\U0001f916 このコメントは <a href="https://github.com/kenkenji/architecture-tracker">'
        f"Architecture Tracker</a> が自動投稿しました（変更検出: {method_label}）</sub>"
    )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="変更提案コメントを生成する")
    parser.add_argument("--proposals", required=True,
                        help="提案のJSON文字列またはファイルパス")
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--detection-method", default="static")
    parser.add_argument("--output-file", default="",
                        help="出力先ファイルパス（未指定時はstdout）")
    args = parser.parse_args()

    if args.proposals.startswith("["):
        proposals = json.loads(args.proposals)
    else:
        with open(args.proposals, encoding="utf-8") as f:
            proposals = json.load(f)

    if not proposals:
        print("No proposals to post.", file=sys.stderr)
        sys.exit(0)

    comment = build_comment(proposals, args.pr_number, args.detection_method)

    if args.output_file:
        with open(args.output_file, "w", encoding="utf-8") as f:
            f.write(comment)
        print(f"Comment written to {args.output_file}", file=sys.stderr)
    else:
        print(comment)


if __name__ == "__main__":
    main()
