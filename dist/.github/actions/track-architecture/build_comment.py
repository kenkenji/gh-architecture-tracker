"""components.yaml からチェックボックス付きコメント本文を生成する。"""

import argparse
import json
import sys
import yaml


# record-mapping/parse_checkboxes.py の NO_IMPACT_ID と同期が必要
NO_IMPACT_ID = "__no_impact__"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--pr-title", required=True)
    parser.add_argument("--components-file", required=True, help="Path to components.yaml")
    parser.add_argument(
        "--ai-components",
        default="",
        help="JSON array of AI-suggested component IDs (empty = manual mode)",
    )
    parser.add_argument(
        "--no-impact-default",
        action="store_true",
        help="Pre-check the no-impact checkbox (used when AI finds 0 components)",
    )
    return parser.parse_args()


def build_checkbox_section(data, ai_component_ids=None):
    if ai_component_ids is None:
        ai_component_ids = set()
    else:
        ai_component_ids = set(ai_component_ids)

    components = data.get("components", [])

    systems = {c["id"]: c["name"] for c in components if c.get("level") == "system"}
    containers = [c for c in components if c.get("level") == "container"]
    component_items = [c for c in components if c.get("level") == "component"]

    lines = []
    for container in containers:
        system_label = ""
        parent = container.get("parent")
        if parent and parent in systems:
            system_label = f" ({systems[parent]})"

        lines.append(f"### {container['name']}{system_label}")
        lines.append("")

        check = "x" if container["id"] in ai_component_ids else " "
        lines.append(f"- [{check}] `{container['id']}` — {container['name']}")

        children = [c for c in component_items if c.get("parent") == container["id"]]
        for child in children:
            check = "x" if child["id"] in ai_component_ids else " "
            lines.append(f"  - [{check}] `{child['id']}` — {child['name']}")

        lines.append("")

    return "\n".join(lines)


def build_no_impact_line(checked=False):
    mark = "x" if checked else " "
    return f"- [{mark}] `{NO_IMPACT_ID}` — 影響なし（このPRはアーキテクチャに影響しません）"


def build_comment(pr_number, pr_title, data, source="manual", ai_component_ids=None,
                  no_impact_default=False):
    checkbox_section = build_checkbox_section(data, ai_component_ids)

    if source == "ai":
        intro = "AIがこのPRの影響コンポーネントを提案しました（✅ = AI提案済み）。必要に応じて修正してください。"
    else:
        intro = "このPRが影響したコンポーネントを選択してください。"

    ai_marker = ""
    if source == "ai" and ai_component_ids:
        ai_marker = f"\n<!-- ai-components: {json.dumps(ai_component_ids, ensure_ascii=False)} -->"

    no_impact_line = build_no_impact_line(checked=no_impact_default)

    return f"""\
## 🏗 Architecture Tracker

<!-- source: {source} -->{ai_marker}

**PR #{pr_number}**: {pr_title}

{intro}

{checkbox_section}
### その他

{no_impact_line}

---
<sub>🤖 このコメントは <a href="https://github.com/kenkenji/architecture-tracker">Architecture Tracker</a> が自動投稿しました</sub>"""


def main():
    args = parse_args()
    with open(args.components_file, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or "components" not in data:
        print("::error::Invalid components.yaml: 'components' key not found", file=sys.stderr)
        sys.exit(1)

    ai_ids = []
    source = "manual"
    if args.ai_components:
        ai_ids = json.loads(args.ai_components)
        if ai_ids:
            source = "ai"

    print(build_comment(args.pr_number, args.pr_title, data, source=source,
                        ai_component_ids=ai_ids, no_impact_default=args.no_impact_default))


if __name__ == "__main__":
    main()
