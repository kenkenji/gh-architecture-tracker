#!/usr/bin/env python3
"""
PRの変更ファイルからアーキテクチャモデルの変更候補を検出する。
LLMベースの分析により、PR本文と変更ファイルから構造変更を提案する。
"""

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import traceback

import yaml

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_OPENAI_MODEL = "gpt-4o"

SKIP_PATTERNS = [
    "docs/**",
    "*.md",
    "**/*.test.*",
    "**/*.spec.*",
    "**/__tests__/**",
]

ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


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
    return {c.get("id") for c in components_data.get("components", []) if c.get("id")}


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


def validate_llm_results(llm_result, existing_ids):
    proposals = []

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

        proposals.append({
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
            "source": "llm",
        })

    for rel in llm_result.get("new_relations", []):
        from_id = rel.get("from", "")
        to_id = rel.get("to", "")
        if not from_id or not to_id:
            continue

        all_ids = existing_ids | {p["id"] for p in proposals if p.get("action") == "add" and "id" in p}
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


def build_llm_prompt(template, pr_body, pr_files, components_data):
    components_text = format_components_for_prompt(components_data)
    files_text = "\n".join(f"- `{f['filename']}` ({f.get('status', 'modified')})" for f in pr_files)

    return (template
            .replace("{components}", components_text)
            .replace("{pr_description}", pr_body or "(PR Descriptionなし)")
            .replace("{changed_files}", files_text))


def format_components_for_prompt(components_data):
    components = components_data.get("components", [])
    relations = components_data.get("relations", [])

    lines = []
    lines.append("### コンポーネント一覧")
    for c in components:
        comp_id = c.get("id", "")
        level = c.get("level", "")
        if not comp_id or not level:
            continue
        parent = c.get("parent", "")
        tech = c.get("technology", "")
        tags = c.get("tags", [])
        tags_str = f" [tags: {', '.join(tags)}]" if tags else ""
        lines.append(
            f"- `{comp_id}` ({level}, parent: {parent}): "
            f"{c.get('name', '')} — {c.get('description', '')} [{tech}]{tags_str}")

    if relations:
        lines.append("")
        lines.append("### 既存のRelation（関係）")
        for r in relations:
            r_from = r.get("from", "")
            r_to = r.get("to", "")
            if not r_from or not r_to:
                continue
            lines.append(
                f"- `{r_from}` → `{r_to}`: {r.get('description', '')} [{r.get('technology', '')}]")

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
                        help="検出モード (static: LLMスキップ, full: LLMで検出)")
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
            "detection_method": "skip",
            "skipped_reason": "doc_or_test_only",
        }))
        return

    if args.mode == "full":
        provider = detect_provider()

        if provider:
            template = ""
            if args.prompt_template:
                with open(args.prompt_template, encoding="utf-8") as f:
                    template = f.read()

            if template:
                prompt = build_llm_prompt(
                    template, pr_body, pr_files, components_data)
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
                    proposals = validate_llm_results(llm_result, existing_ids)

                    print(json.dumps({
                        "proposals": proposals,
                        "detection_method": "llm",
                        "provider": provider,
                    }, ensure_ascii=False))
                    return

                except Exception as e:
                    print(f"LLM call failed: {e}\n{traceback.format_exc()}", file=sys.stderr)

    print(json.dumps({
        "proposals": [],
        "detection_method": "none",
    }))


if __name__ == "__main__":
    main()
