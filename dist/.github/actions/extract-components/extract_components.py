#!/usr/bin/env python3
"""
LLM APIを呼び出してPR Descriptionから影響コンポーネントを抽出するスクリプト。

環境変数 ANTHROPIC_API_KEY、OPENAI_API_KEY、CLAUDE_CODE_OAUTH_TOKEN の
いずれかが設定されている場合に対応するプロバイダーを使用する。
優先順位: ANTHROPIC_API_KEY > OPENAI_API_KEY > CLAUDE_CODE_OAUTH_TOKEN
"""

import json
import os
import re
import subprocess
import sys
import argparse
import time

import yaml

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_OPENAI_MODEL = "gpt-4o"


def format_components_for_prompt(components_data):
    """components.yamlのデータをプロンプト用テキストに整形する"""
    components = components_data["components"]
    systems = [c for c in components if c["level"] == "system"]
    containers = [c for c in components if c["level"] == "container"]
    comps = [c for c in components if c["level"] == "component"]

    children_map = {}
    for c in comps:
        parent = c.get("parent", "")
        children_map.setdefault(parent, []).append(c["id"])

    lines = []

    lines.append("#### Systems（選択対象外）")
    for s in systems:
        tags = ", ".join(s.get("tags", []))
        lines.append(f"- `{s['id']}`: {s['name']} — {s['description']} [tags: {tags}]")

    lines.append("")
    lines.append("#### Containers")
    for c in containers:
        parent = c.get("parent", "")
        tech = c.get("technology", "")
        children = children_map.get(c["id"], [])
        children_note = ", ".join(children) if children else "なし"
        lines.append(
            f"- `{c['id']}` (parent: {parent}): {c['name']} — {c['description']} [{tech}]"
        )
        lines.append(f"  子Component: {children_note}")

    lines.append("")
    lines.append("#### Components")
    for c in comps:
        parent = c.get("parent", "")
        tech = c.get("technology", "")
        lines.append(
            f"- `{c['id']}` (parent: {parent}): {c['name']} — {c['description']} [{tech}]"
        )

    return "\n".join(lines)


def build_prompt(template, components_text, pr_description):
    """プロンプトテンプレートにコンポーネント一覧とPR Descriptionを埋め込む"""
    return template.replace("{components}", components_text).replace(
        "{pr_description}", pr_description
    )


def parse_llm_response(response_text):
    """LLMレスポンスからJSONを抽出してパースする"""
    text = response_text.strip()

    json_match = re.search(r"\{[\s\S]*\}", text)
    if json_match:
        return json.loads(json_match.group())

    return json.loads(text)


def validate_component_ids(component_ids, components_data):
    """LLMが返したコンポーネントIDがcomponents.yamlに実在するか検証する。
    未知のID・systemレベルのID・重複を除外し、有効なIDのみ返す。"""
    valid_ids = {
        c["id"] for c in components_data["components"]
        if c["level"] in ("container", "component")
    }
    seen = set()
    validated = []
    for cid in component_ids:
        if cid not in valid_ids:
            print(f"Warning: invalid component ID '{cid}' returned by LLM, skipping", file=sys.stderr)
        elif cid in seen:
            print(f"Warning: duplicate component ID '{cid}' returned by LLM, skipping", file=sys.stderr)
        else:
            seen.add(cid)
            validated.append(cid)
    return validated


def detect_provider():
    """環境変数からLLMプロバイダーを自動検出する。
    優先順位: ANTHROPIC_API_KEY > OPENAI_API_KEY > CLAUDE_CODE_OAUTH_TOKEN"""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "claude-code"
    return None


def call_anthropic(prompt, model=None, max_retries=2):
    """Anthropic Claude APIを呼び出す。レート制限時にリトライする。"""
    import anthropic

    model = model or DEFAULT_ANTHROPIC_MODEL
    client = anthropic.Anthropic(timeout=60.0)
    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
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


def call_claude_code(prompt, model=None, max_retries=2):
    """Claude Code CLIを呼び出す。CLAUDE_CODE_OAUTH_TOKENで認証する。"""
    model = model or DEFAULT_ANTHROPIC_MODEL
    cmd = ["claude", "-p", prompt, "--output-format", "text", "--model", model]

    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                if attempt < max_retries:
                    wait = 2 ** (attempt + 1)
                    print(
                        f"CLI exited with code {result.returncode}, retrying in {wait}s...",
                        file=sys.stderr,
                    )
                    time.sleep(wait)
                    continue
                raise RuntimeError(
                    f"claude CLI exited with code {result.returncode}: {result.stderr.strip()}"
                )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                wait = 2 ** (attempt + 1)
                print(f"CLI timeout, retrying in {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise RuntimeError("claude CLI timed out after all retries")


def call_openai(prompt, model=None, max_retries=2):
    """OpenAI APIを呼び出す。レート制限時にリトライする。"""
    import openai

    model = model or DEFAULT_OPENAI_MODEL
    client = openai.OpenAI(timeout=60.0)
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=1024,
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


def main():
    parser = argparse.ArgumentParser(description="LLMでPR Descriptionからコンポーネントを抽出する")
    parser.add_argument(
        "--components-file",
        required=True,
        help="components.yamlファイルのパス",
    )
    parser.add_argument(
        "--pr-body-file",
        required=True,
        help="PR Descriptionが書かれたファイルのパス",
    )
    parser.add_argument(
        "--prompt-template",
        required=True,
        help="プロンプトテンプレートファイルのパス",
    )
    parser.add_argument(
        "--provider",
        choices=["anthropic", "openai", "claude-code"],
        help="LLMプロバイダー（未指定時は環境変数から自動検出）",
    )
    parser.add_argument(
        "--model",
        help="モデル名（デフォルト: プロバイダーに応じた標準モデル）",
    )
    args = parser.parse_args()

    provider = args.provider or detect_provider()

    if provider is None:
        result = {
            "components": [],
            "reasoning": "",
            "provider": "",
            "skipped": "true",
        }
        print(json.dumps(result))
        print(
            "No API key found. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or CLAUDE_CODE_OAUTH_TOKEN.",
            file=sys.stderr,
        )
        return

    with open(args.components_file, encoding="utf-8") as f:
        components_data = yaml.safe_load(f)

    with open(args.pr_body_file, encoding="utf-8") as f:
        pr_description = f.read()

    with open(args.prompt_template, encoding="utf-8") as f:
        template = f.read()

    components_text = format_components_for_prompt(components_data)
    prompt = build_prompt(template, components_text, pr_description)

    try:
        model = args.model or None
        if provider == "anthropic":
            raw_response = call_anthropic(prompt, model)
        elif provider == "openai":
            raw_response = call_openai(prompt, model)
        else:
            raw_response = call_claude_code(prompt, model)

        parsed = parse_llm_response(raw_response)
        raw_components = parsed.get("affected_components", [])
        components = validate_component_ids(raw_components, components_data)
        reasoning = parsed.get("reasoning", "")

        result = {
            "components": components,
            "reasoning": reasoning,
            "provider": provider,
            "skipped": "false",
        }
        print(json.dumps(result, ensure_ascii=False))

    except json.JSONDecodeError as e:
        print(f"Failed to parse LLM response as JSON: {e}", file=sys.stderr)
        result = {
            "components": [],
            "reasoning": f"JSON parse error: {e}",
            "provider": provider,
            "skipped": "false",
        }
        print(json.dumps(result))
        sys.exit(1)

    except Exception as e:
        print(f"LLM API call failed: {e}", file=sys.stderr)
        result = {
            "components": [],
            "reasoning": f"API error: {e}",
            "provider": provider,
            "skipped": "false",
        }
        print(json.dumps(result))
        sys.exit(1)


if __name__ == "__main__":
    main()
