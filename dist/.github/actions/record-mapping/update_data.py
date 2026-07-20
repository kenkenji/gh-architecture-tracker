"""mappings.jsonとtimeline.jsonを更新する。"""

import argparse
import json
import sys
from datetime import datetime, timezone


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mappings-file", required=True)
    parser.add_argument("--timeline-file", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--pr-title", required=True)
    parser.add_argument("--pr-url", required=True)
    parser.add_argument("--merged-at", required=True)
    parser.add_argument("--components", required=True, help="JSON array of component ids")
    parser.add_argument("--author", required=True)
    parser.add_argument("--source", default="manual", choices=["manual", "ai"])
    parser.add_argument("--ai-components", default="", help="JSON array of AI-suggested component ids")
    parser.add_argument("--no-impact", action="store_true", help="Mark as intentionally no impact")
    return parser.parse_args()


def update_mappings(data, pr_number, pr_title, pr_url, merged_at, components,
                    author, timestamp, source="manual", ai_components=None,
                    no_impact=False):
    if components and no_impact:
        no_impact = False
    entry = {
        "pr_number": pr_number,
        "pr_title": pr_title,
        "pr_url": pr_url,
        "merged_at": merged_at,
        "components": components,
        "source": source,
        "author": author,
        "timestamp": timestamp,
    }
    if ai_components is not None:
        entry["ai_components"] = ai_components
    if no_impact:
        entry["no_impact"] = True
    data["mappings"][str(pr_number)] = entry
    return data


def update_timeline(data, pr_number, pr_title, pr_url, components,
                    author, timestamp, source="manual", ai_components=None,
                    no_impact=False):
    if components and no_impact:
        no_impact = False
    compact = timestamp.replace("-", "").replace(":", "").replace(".", "")
    entry_id = f"pr-{pr_number}-{compact}"

    entry = {
        "id": entry_id,
        "timestamp": timestamp,
        "pr_number": pr_number,
        "pr_title": pr_title,
        "pr_url": pr_url,
        "components": components,
        "source": source,
        "author": author,
    }
    if ai_components is not None:
        entry["ai_components"] = ai_components
    if no_impact:
        entry["no_impact"] = True

    data["entries"].insert(0, entry)
    return data


def main():
    args = parse_args()
    components = json.loads(args.components)
    ai_components = json.loads(args.ai_components) if args.ai_components else None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(args.mappings_file, encoding="utf-8") as f:
        mappings_data = json.load(f)

    with open(args.timeline_file, encoding="utf-8") as f:
        timeline_data = json.load(f)

    mappings_data = update_mappings(
        mappings_data, args.pr_number, args.pr_title, args.pr_url,
        args.merged_at, components, args.author, now,
        source=args.source, ai_components=ai_components,
        no_impact=args.no_impact,
    )
    timeline_data = update_timeline(
        timeline_data, args.pr_number, args.pr_title, args.pr_url,
        components, args.author, now,
        source=args.source, ai_components=ai_components,
        no_impact=args.no_impact,
    )

    with open(args.mappings_file, "w", encoding="utf-8") as f:
        json.dump(mappings_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    with open(args.timeline_file, "w", encoding="utf-8") as f:
        json.dump(timeline_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"Updated mappings and timeline for PR #{args.pr_number}")
    print(f"Components: {components}")


if __name__ == "__main__":
    main()
