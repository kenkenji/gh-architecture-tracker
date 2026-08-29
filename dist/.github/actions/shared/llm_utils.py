"""
LLM API呼び出しの共通ユーティリティ。

extract_components.py と detect_changes.py で共有する関数群。
"""

import json
import os
import subprocess
import sys
import time

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_OPENAI_MODEL = "gpt-4o"


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


def call_anthropic(prompt, model=None, max_tokens=1024, max_retries=2):
    """Anthropic Claude APIを呼び出す。レート制限時にリトライする。"""
    import anthropic

    model = model or DEFAULT_ANTHROPIC_MODEL
    client = anthropic.Anthropic(timeout=60.0)
    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
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


def call_openai(prompt, model=None, max_tokens=1024, max_retries=2):
    """OpenAI APIを呼び出す。レート制限時にリトライする。"""
    import openai

    model = model or DEFAULT_OPENAI_MODEL
    client = openai.OpenAI(timeout=60.0)
    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
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


def parse_llm_response(response_text):
    """LLMレスポンスからJSONを抽出してパースする。
    raw_decode()で最初のJSONオブジェクトのみをパースし、
    後続のテキストやJSONブロックは無視する。"""
    text = response_text.strip()

    decoder = json.JSONDecoder()
    idx = text.find("{")
    if idx != -1:
        result, _ = decoder.raw_decode(text, idx)
        return result

    return json.loads(text)


def call_llm(provider, prompt, model=None, max_tokens=1024):
    """プロバイダーに応じたLLM APIを呼び出す。"""
    if provider == "anthropic":
        return call_anthropic(prompt, model, max_tokens=max_tokens)
    elif provider == "openai":
        return call_openai(prompt, model, max_tokens=max_tokens)
    elif provider == "claude-code":
        if max_tokens != 1024:
            print(
                f"Warning: max_tokens={max_tokens} is ignored for claude-code provider (CLI has no --max-tokens flag)",
                file=sys.stderr,
            )
        return call_claude_code(prompt, model)
    else:
        raise ValueError(f"Unknown provider: {provider}")
