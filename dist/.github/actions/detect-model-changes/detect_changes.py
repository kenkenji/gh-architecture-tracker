#!/usr/bin/env python3
"""
PRの変更ファイルからアーキテクチャモデルの変更候補を検出する。
静的解析（ファイルパスパターンマッチ）+ オプションのLLM判断。
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time

import yaml

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_OPENAI_MODEL = "gpt-4o"

PATH_RULES = [
    {
        "pattern": "spa/src/pages/*.tsx",
        "suggestion": "add_component",
        "parent": "web-app",
        "level": "component",
        "technology": "React + TypeScript",
        "description_template": "{name} ページコンポーネント",
    },
    {
        "pattern": "spa/src/context/*.tsx",
        "suggestion": "add_component",
        "parent": "web-app",
        "level": "component",
        "technology": "React Context",
        "description_template": "{name} コンテキスト",
    },
    {
        "pattern": "spa/src/api/*.ts",
        "suggestion": "add_component",
        "parent": "web-app",
        "level": "component",
        "technology": "TypeScript",
        "description_template": "{name} APIモジュール",
    },
    {
        "pattern": ".github/actions/*/action.yml",
        "suggestion": "add_component",
        "parent": "github-actions",
        "level": "component",
        "technology": "Python",
        "description_template": "{name} Composite Action",
    },
    {
        "pattern": ".github/workflows/*.yml",
        "suggestion": "modify_component",
        "target": "pr-trigger",
        "field": "technology",
    },
]

SKIP_PATTERNS = [
    "docs/**",
    "*.md",
    "**/*.test.*",
    "**/*.spec.*",
    "**/__tests__/**",
]

ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def pascal_to_kebab(name):
    s = re.sub(r"([A-Z])", r"-\1", name).lower().lstrip("-")
    return re.sub(r"-+", "-", s)


def file_to_component_id(filepath, rule):
    if rule["pattern"].startswith(".github/actions/"):
        parts = filepath.replace("\\", "/").split("/")
        idx = parts.index("actions") if "actions" in parts else -1
        if idx >= 0 and idx + 1 < len(parts):
            return parts[idx + 1]
        return None

    basename = filepath.replace("\\", "/").split("/")[-1]
    name_part = basename.rsplit(".", 1)[0]
    return pascal_to_kebab(name_part)


def file_to_human_name(filepath, rule):
    if rule["pattern"].startswith(".github/actions/"):
        parts = filepath.replace("\\", "/").split("/")
        idx = parts.index("actions") if "actions" in parts else -1
        if idx >= 0 and idx + 1 < len(parts):
            return parts[idx + 1]
        return None

    basename = filepath.replace("\\", "/").split("/")[-1]
    name_part = basename.rsplit(".", 1)[0]
    return name_part


def is_doc_or_test_only(files):
    for f in files:
        path = f["filename"]
        matched = False
        for pat in SKIP_PATTERNS:
            if fnmatch.fnmatch(path, pat):
                matched = True
                break
        if not matched:
            return False
    return True


def get_existing_ids(components_data):
    return {c["id"] for c in components_data.get("components", [])}


def static_analysis(pr_files, components_data):
    existing_ids = get_existing_ids(components_data)
    candidates = []

    for f in pr_files:
        filepath = f["filename"]
        status = f.get("status", "modified")

        for rule in PATH_RULES:
            if not fnmatch.fnmatch(filepath, rule["pattern"]):
                continue

            if rule["suggestion"] == "add_component":
                if status in ("added", "modified"):
                    comp_id = file_to_component_id(filepath, rule)
                    if comp_id and comp_id not in existing_ids:
                        human_name = file_to_human_name(filepath, rule)
                        desc = rule["description_template"].format(name=human_name)
                        candidates.append({
                            "action": "add",
                            "target": "component",
                            "id": comp_id,
                            "name": human_name,
                            "level": rule["level"],
                            "parent": rule["parent"],
                            "technology": rule["technology"],
                            "description": desc,
                            "source_file": filepath,
                            "confidence": "medium",
                            "source": "static",
                            "reasoning": f"新規ファイル {filepath} がパターン {rule['pattern']} にマッチ",
                        })

                elif status == "removed":
                    comp_id = file_to_component_id(filepath, rule)
                    if comp_id and comp_id in existing_ids:
                        candidates.append({
                            "action": "remove",
                            "target": "component",
                            "id": comp_id,
                            "source_file": filepath,
                            "confidence": "medium",
                            "source": "static",
                            "reasoning": f"ファイル {filepath} が削除されたため",
                        })

            elif rule["suggestion"] == "modify_component":
                pass

    seen_ids = set()
    deduped = []
    for c in candidates:
        if c["id"] not in seen_ids:
            seen_ids.add(c["id"])
            deduped.append(c)
    return deduped


def detect_provider():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "claude-code"
    return None


def call_anthropic(prompt, model=None, max_retries=2):
    import anthropic

    model = model or DEFAULT_ANTHROPIC_MODEL
    client = anthropic.Anthropic(timeout=60.0)
    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text
        except anthropic.RateLimitError:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print(f"Rate limited, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise
        except anthropic.APITimeoutError:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print(f"Timeout, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise


def call_openai(prompt, model=None, max_retries=2):
    import openai

    model = model or DEFAULT_OPENAI_MODEL
    client = openai.OpenAI(timeout=60.0)
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content
        except openai.RateLimitError:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print(f"Rate limited, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise
        except openai.APITimeoutError:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print(f"Timeout, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise


def call_claude_code(prompt, model=None, max_retries=2):
    model = model or DEFAULT_ANTHROPIC_MODEL
    cmd = ["claude", "-p", prompt, "--output-format", "text", "--model", model]

    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                if attempt < max_retries:
                    wait = 2 ** (attempt + 1)
                    print(f"CLI exited with code {result.returncode}, retrying in {wait}s...",
                          file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise RuntimeError(
                    f"claude CLI exited with code {result.returncode}: {result.stderr.strip()}")
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print(f"CLI timeout, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise RuntimeError("claude CLI timed out after all retries")


def parse_llm_response(response_text):
    text = response_text.strip()
    json_match = re.search(r"\{[\s\S]*\}", text)
    if json_match:
        return json.loads(json_match.group())
    return json.loads(text)


def sanitize_string(value, max_length=200):
    if not isinstance(value, str):
        return str(value)[:max_length]
    return value.strip()[:max_length]


def validate_id(comp_id):
    if not comp_id or not isinstance(comp_id, str):
        return False
    return bool(ID_PATTERN.match(comp_id))


def merge_llm_results(static_candidates, llm_result, existing_ids):
    proposals = []
    static_by_id = {c["id"]: c for c in static_candidates}

    for p in llm_result.get("proposals", []):
        if p.get("confidence") == "low":
            print(f"Skipping low-confidence proposal: {p.get('id', 'unknown')}", file=sys.stderr)
            continue

        comp_id = p.get("id", "")
        if not validate_id(comp_id):
            print(f"Warning: invalid ID '{comp_id}' from LLM, skipping", file=sys.stderr)
            continue

        action = p.get("action", "add")

        if action == "add" and comp_id in existing_ids:
            continue
        if action == "remove" and comp_id not in existing_ids:
            continue

        proposal = {
            "action": action,
            "target": p.get("target", "component"),
            "id": comp_id,
            "name": sanitize_string(p.get("name", comp_id)),
            "level": p.get("level", "component"),
            "parent": sanitize_string(p.get("parent", "")),
            "technology": sanitize_string(p.get("technology", "")),
            "description": sanitize_string(p.get("description", "")),
            "confidence": p.get("confidence", "medium"),
            "reasoning": sanitize_string(p.get("reasoning", "")),
        }

        if comp_id in static_by_id:
            static = static_by_id.pop(comp_id)
            proposal["source_file"] = static.get("source_file", "")
            proposal["source"] = "static+llm"
        else:
            proposal["source"] = "llm"

        proposals.append(proposal)

    for comp_id, static in static_by_id.items():
        proposals.append(static)

    for rel in llm_result.get("new_relations", []):
        from_id = rel.get("from", "")
        to_id = rel.get("to", "")
        if not from_id or not to_id:
            continue

        all_ids = existing_ids | {p["id"] for p in proposals if p.get("action") == "add"}
        if from_id not in all_ids or to_id not in all_ids:
            print(f"Warning: relation {from_id} -> {to_id} references unknown component, skipping",
                  file=sys.stderr)
            continue

        proposals.append({
            "action": "add",
            "target": "relation",
            "from": from_id,
            "to": to_id,
            "description": sanitize_string(rel.get("description", "")),
            "technology": sanitize_string(rel.get("technology", "")),
            "reasoning": sanitize_string(rel.get("reasoning", "")),
            "source": "llm",
        })

    return proposals


def build_llm_prompt(template, static_candidates, pr_body, pr_files, components_data):
    components_text = format_components_for_prompt(components_data)
    candidates_text = json.dumps(static_candidates, ensure_ascii=False, indent=2)
    files_text = "\n".join(f"- `{f['filename']}` ({f.get('status', 'modified')})" for f in pr_files)

    return (template
            .replace("{components}", components_text)
            .replace("{candidates}", candidates_text)
            .replace("{pr_description}", pr_body or "(PR Descriptionなし)")
            .replace("{changed_files}", files_text))


def format_components_for_prompt(components_data):
    components = components_data.get("components", [])
    relations = components_data.get("relations", [])

    lines = []
    lines.append("### コンポーネント一覧")
    for c in components:
        parent = c.get("parent", "")
        tech = c.get("technology", "")
        tags = c.get("tags", [])
        tags_str = f" [tags: {', '.join(tags)}]" if tags else ""
        lines.append(
            f"- `{c['id']}` ({c['level']}, parent: {parent}): "
            f"{c.get('name', '')} — {c.get('description', '')} [{tech}]{tags_str}")

    if relations:
        lines.append("")
        lines.append("### 既存のRelation（関係）")
        for r in relations:
            lines.append(
                f"- `{r['from']}` → `{r['to']}`: {r.get('description', '')} [{r.get('technology', '')}]")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="PRからアーキテクチャモデル変更候補を検出する")
    parser.add_argument("--pr-files", required=True,
                        help="PR変更ファイルリストのJSONファイルパス")
    parser.add_argument("--pr-body-file", default="",
                        help="PR Descriptionが書かれたファイルのパス")
    parser.add_argument("--components-file", required=True,
                        help="components.yamlファイルのパス")
    parser.add_argument("--prompt-template", default="",
                        help="LLMプロンプトテンプレートファイルのパス")
    parser.add_argument("--mode", choices=["static", "full"], default="full",
                        help="検出モード (static: 静的解析のみ, full: 静的+LLM)")
    parser.add_argument("--model", default="",
                        help="LLMモデル名")
    args = parser.parse_args()

    with open(args.pr_files, encoding="utf-8") as f:
        pr_files = json.load(f)

    with open(args.components_file, encoding="utf-8") as f:
        components_data = yaml.safe_load(f) or {}

    pr_body = ""
    if args.pr_body_file:
        with open(args.pr_body_file, encoding="utf-8") as f:
            pr_body = f.read()

    if is_doc_or_test_only(pr_files):
        print(json.dumps({
            "proposals": [],
            "detection_method": "static",
            "skipped_reason": "doc_or_test_only",
        }))
        return

    static_candidates = static_analysis(pr_files, components_data)
    detection_method = "static"

    if args.mode == "full" and static_candidates:
        provider = detect_provider()

        if provider:
            template = ""
            if args.prompt_template:
                with open(args.prompt_template, encoding="utf-8") as f:
                    template = f.read()

            if template:
                prompt = build_llm_prompt(
                    template, static_candidates, pr_body, pr_files, components_data)
                model = args.model or None

                try:
                    if provider == "anthropic":
                        raw = call_anthropic(prompt, model)
                    elif provider == "openai":
                        raw = call_openai(prompt, model)
                    else:
                        raw = call_claude_code(prompt, model)

                    llm_result = parse_llm_response(raw)
                    existing_ids = get_existing_ids(components_data)
                    proposals = merge_llm_results(static_candidates, llm_result, existing_ids)
                    detection_method = "static+llm"

                    result = {
                        "proposals": proposals,
                        "detection_method": detection_method,
                        "provider": provider,
                    }
                    print(json.dumps(result, ensure_ascii=False))
                    return

                except Exception as e:
                    print(f"LLM call failed, falling back to static-only: {e}", file=sys.stderr)

    result = {
        "proposals": static_candidates,
        "detection_method": detection_method,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
