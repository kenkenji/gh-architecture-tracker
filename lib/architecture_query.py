#!/usr/bin/env python3
"""
gh architecture-tracker の読み取り系サブコマンド（components / history）の実体。

Claude Code 等の AI エージェントがアーキテクチャモデルを参照できるよう、
`_architecture-tracker` ブランチのデータを GitHub API 経由で読み取り、
人間向けのテーブル形式、または `--json` で構造化データとして出力する。

- データの書き込みは一切行わない（読み取り専用）
- LLM は呼び出さない。Issue との関係の判断は、呼び出した側（エージェント）に任せる
- `--json` の出力形式は FORMAT_VERSION で管理する。フィールドの削除・意味の変更をする場合は上げること
"""

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from urllib.parse import quote

# PyYAML がない場合は main() で案内を出して終了する
# （bash 側で `python -c 'import yaml'` を実行すると、作業ディレクトリの yaml.py が読み込まれてしまうため）
try:
    import yaml
except ImportError:
    yaml = None

DATA_BRANCH = "_architecture-tracker"
FORMAT_VERSION = "1"
REPO_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def _find_actions_dir():
    """Composite Action のディレクトリを探す（schema_utils を共有するため）。

    - インストール済みの gh extension: <extension>/dist/.github/actions
    - このリポジトリで直接実行する場合: <repo>/.github/actions
    """
    override = os.environ.get("ARCHITECTURE_TRACKER_ACTIONS_DIR")
    if override:
        return override
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, os.pardir, "dist", ".github", "actions"),
        os.path.join(here, os.pardir, os.pardir, ".github", "actions"),
    ]
    for c in candidates:
        if os.path.isfile(os.path.join(c, "shared", "schema_utils.py")):
            return os.path.normpath(c)
    return None


ACTIONS_DIR = _find_actions_dir()
if ACTIONS_DIR:
    sys.path.insert(0, os.path.join(ACTIONS_DIR, "shared"))


class QueryError(Exception):
    """利用者に表示して終了すべきエラー"""


# --- データ取得 ---


def run_gh_api(path):
    """gh api を実行して本文を返す。404 の場合は None を返す。"""
    cmd = ["gh", "api", "-H", "Accept: application/vnd.github.raw", path]
    try:
        # デコードは自前で行う。subprocess に任せると、UTF-8 でない応答が Windows では
        # 例外にならず stdout が None（＝空のデータ）になってしまう
        result = subprocess.run(cmd, capture_output=True, timeout=60)
    except FileNotFoundError:
        raise QueryError("gh コマンドが見つかりません。GitHub CLI をインストールしてください。")
    except subprocess.TimeoutExpired:
        raise QueryError(f"GitHub API の呼び出しがタイムアウトしました: {path}")
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        if "HTTP 404" in stderr:
            return None
        raise QueryError(f"GitHub API の呼び出しに失敗しました: {path}\n{stderr}")
    try:
        return (result.stdout or b"").decode("utf-8")
    except UnicodeDecodeError as e:
        raise QueryError(f"GitHub API の応答が UTF-8 ではありません: {path}（{e}）")


def fetch_data_file(repo, name, branch=DATA_BRANCH):
    return run_gh_api(f"repos/{repo}/contents/{name}?ref={quote(branch, safe='')}")


def _parse_data(name, text, parser):
    try:
        return parser(text)
    except (json.JSONDecodeError, yaml.YAMLError) as e:
        raise QueryError(f"{name} を解析できませんでした: {e}")


def load_components(repo, branch=DATA_BRANCH):
    text = fetch_data_file(repo, "components.yaml", branch)
    if text is None:
        # リポジトリ自体が見えない場合も 404 になるため、原因を切り分ける
        if run_gh_api(f"repos/{repo}") is None:
            raise QueryError(
                f"リポジトリ {repo} が見つかりません。名前の誤りか、アクセス権がない可能性があります。"
            )
        raise QueryError(
            f"{repo} の {branch} ブランチに components.yaml が見つかりません。"
            "'gh architecture-tracker init' でセットアップ済みか確認してください。"
        )
    data = _parse_data("components.yaml", text, yaml.safe_load) or {}
    if not isinstance(data, dict):
        raise QueryError("components.yaml の形式が不正です。")
    warn_unknown_schema(data)
    return data


def load_mappings(repo, branch=DATA_BRANCH):
    text = fetch_data_file(repo, "mappings.json", branch)
    if not text or not text.strip():
        return {}
    data = _parse_data("mappings.json", text, json.loads)
    # 形式が崩れている場合に「PR 0件」として表示しないよう、エラーにする
    if not isinstance(data, dict) or not isinstance(data.get("mappings", {}), dict):
        raise QueryError("mappings.json の形式が不正です（mappings が PR 番号をキーとするオブジェクトではありません）。")
    return data


def warn_unknown_schema(components_data):
    from schema_utils import SUPPORTED_SCHEMA_VERSIONS, get_schema_version

    version = get_schema_version(components_data)
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        print(
            f"⚠ components.yaml の schema_version \"{version}\" は未知のバージョンです。"
            "gh extension upgrade architecture-tracker で拡張を更新してください。",
            file=sys.stderr,
        )


# --- 集計 ---


def _sort_key(mapping):
    # 型が崩れた値（数値など）が混ざっても比較できるよう文字列にする
    return str(mapping.get("merged_at") or mapping.get("timestamp") or "")


def mapped_components(mapping):
    """エントリの components を返す。リストでない場合は空とみなす"""
    comps = mapping.get("components")
    if not isinstance(comps, list):
        return []
    return [c for c in comps if isinstance(c, str)]


def iter_mappings(mappings_data):
    """mappings.json のエントリを新しい順に返す"""
    entries = [
        m for m in (mappings_data.get("mappings") or {}).values() if isinstance(m, dict)
    ]
    return sorted(entries, key=_sort_key, reverse=True)


def count_prs_by_component(mappings_data):
    counts = {}
    for m in iter_mappings(mappings_data):
        for cid in set(mapped_components(m)):
            counts[cid] = counts.get(cid, 0) + 1
    return counts


def component_index(components_data):
    return {
        c["id"]: c
        for c in components_data.get("components") or []
        if isinstance(c, dict) and c.get("id")
    }


def model_version(components_data):
    value = components_data.get("version")
    return "" if value is None else str(value)


def build_components_output(components_data, mappings_data):
    from schema_utils import get_schema_version

    counts = count_prs_by_component(mappings_data)
    components = []
    for c in component_index(components_data).values():
        components.append({
            "id": c["id"],
            "name": c.get("name", ""),
            "description": c.get("description", ""),
            "level": c.get("level", ""),
            "parent": c.get("parent", ""),
            "technology": c.get("technology", ""),
            "tags": c.get("tags") or [],
            "pr_count": counts.get(c["id"], 0),
        })
    relations = [
        {
            "from": r.get("from", ""),
            "to": r.get("to", ""),
            "description": r.get("description", ""),
            "technology": r.get("technology", ""),
        }
        for r in components_data.get("relations") or []
        if isinstance(r, dict) and r.get("from") and r.get("to")
    ]
    return {
        "format_version": FORMAT_VERSION,
        "model_version": model_version(components_data),
        "schema_version": get_schema_version(components_data),
        "components": components,
        "relations": relations,
    }


def build_history_output(component_id, components_data, mappings_data, limit=None):
    index = component_index(components_data)
    matched = [
        m for m in iter_mappings(mappings_data)
        if component_id in mapped_components(m)
    ]
    if component_id not in index:
        if not matched:
            raise QueryError(
                f"コンポーネント '{component_id}' は定義されていません。"
                "'gh architecture-tracker components' で ID を確認してください。"
            )
        print(
            f"⚠ コンポーネント '{component_id}' は現在のモデルに存在しません（過去のマッピングのみ表示します）。",
            file=sys.stderr,
        )

    history = []
    for m in matched[:limit] if limit else matched:
        entry = {
            "pr_number": m.get("pr_number"),
            "title": m.get("pr_title", ""),
            "pr_url": m.get("pr_url", ""),
            "merged_at": m.get("merged_at", ""),
            # author は mappings.json の値をそのまま出す。記録した経路により意味が異なる
            # （マージ時の自動記録・catch-up では PR 作成者、チェックボックスでの確定では操作したユーザー、
            #   自動承認では "auto-approve"）
            "author": m.get("author", ""),
            "source": m.get("source", ""),
        }
        if m.get("source_repo"):
            entry["source_repo"] = m["source_repo"]
        history.append(entry)

    return {
        "format_version": FORMAT_VERSION,
        "component": {
            "id": component_id,
            "name": index.get(component_id, {}).get("name", component_id),
        },
        "history": history,
        "total_prs": len(matched),
        "model_version": model_version(components_data),
    }


# --- テーブル出力 ---


def sanitize(value):
    """端末に表示する文字列から制御文字を取り除く。

    PR タイトル等の第三者が書いた文字列経由で、エスケープシーケンス（OSC 52 / OSC 8 等）や
    双方向制御文字を端末に送り込まれないようにする。改行・タブは空白にする。
    """
    text = "" if value is None else str(value)
    out = []
    for ch in text:
        if ch in "\r\n\t":
            out.append(" ")
        elif unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def display_width(text):
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def format_table(headers, rows):
    cells = [[sanitize(h) for h in headers]] + [[sanitize(v) for v in row] for row in rows]
    widths = [max(display_width(r[i]) for r in cells) for i in range(len(headers))]

    def fmt(row):
        padded = [v + " " * (widths[i] - display_width(v)) for i, v in enumerate(row)]
        return "  ".join(padded).rstrip()

    lines = [fmt(cells[0]), "  ".join("-" * w for w in widths)]
    lines.extend(fmt(r) for r in cells[1:])
    return "\n".join(lines)


def _date(value):
    return str(value or "")[:10]


def _pr_label(entry):
    num = entry.get("pr_number")
    repo = entry.get("source_repo")
    return f"{repo}#{num}" if repo else f"#{num}"


def render_components(output):
    s = sanitize
    lines = [
        f"モデルバージョン: {s(output['model_version']) or '-'}"
        f"（schema_version: {s(output['schema_version'])}）",
        "",
    ]
    if not output["components"]:
        lines.append("コンポーネントは定義されていません。")
        return "\n".join(lines)
    rows = [
        [c["id"], c["name"], c["level"], c["parent"], c["technology"], c["pr_count"]]
        for c in output["components"]
    ]
    lines.append(format_table(["ID", "NAME", "LEVEL", "PARENT", "TECHNOLOGY", "PRS"], rows))
    lines.append("")
    lines.append(f"{len(output['components'])}件のコンポーネント / {len(output['relations'])}件の関係")
    return "\n".join(lines)


def render_history(output):
    s = sanitize
    comp = output["component"]
    lines = [f"{s(comp['name'])} ({s(comp['id'])}) の変更履歴: {output['total_prs']}件", ""]
    if not output["history"]:
        lines.append("このコンポーネントに紐付いたPRはありません。")
        return "\n".join(lines)
    rows = [
        [_pr_label(h), _date(h["merged_at"]), h["source"], h["author"], h["title"]]
        for h in output["history"]
    ]
    lines.append(format_table(["PR", "MERGED", "SOURCE", "AUTHOR", "TITLE"], rows))
    if len(output["history"]) < output["total_prs"]:
        lines.append("")
        lines.append(f"（直近 {len(output['history'])}件を表示）")
    return "\n".join(lines)


# --- エントリーポイント ---


def cmd_components(args):
    components_data = load_components(args.repo, args.data_branch)
    mappings_data = load_mappings(args.repo, args.data_branch)
    output = build_components_output(components_data, mappings_data)
    return output, render_components


def cmd_history(args):
    components_data = load_components(args.repo, args.data_branch)
    mappings_data = load_mappings(args.repo, args.data_branch)
    output = build_history_output(args.component, components_data, mappings_data, args.limit)
    return output, render_history


def positive_int(value):
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"整数を指定してください: {value}")
    if n < 1:
        raise argparse.ArgumentTypeError(f"1以上の整数を指定してください: {value}")
    return n


def repo_name(value):
    # "." / ".." は API のパスを辿られるため、文字種が正しくても拒否する
    if not REPO_PATTERN.match(value) or any(part in (".", "..") for part in value.split("/")):
        raise argparse.ArgumentTypeError(f"OWNER/REPO の形式で指定してください: {value}")
    return value


def build_parser():
    # 省略形（--rep 等）を受け付けると、bash 側の --repo 判定と食い違うため無効にする
    parser = argparse.ArgumentParser(prog="gh architecture-tracker", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--repo", required=True, type=repo_name, help="OWNER/REPO")
        p.add_argument("--data-branch", default=DATA_BRANCH)
        p.add_argument("--json", action="store_true", help="JSON で出力する")

    p = sub.add_parser("components", allow_abbrev=False)
    add_common(p)
    p.set_defaults(func=cmd_components)

    p = sub.add_parser("history", allow_abbrev=False)
    add_common(p)
    p.add_argument("--component", required=True)
    p.add_argument("--limit", type=positive_int, default=None)
    p.set_defaults(func=cmd_history)

    return parser


def main(argv=None):
    # Windows 環境でも日本語を含む出力を文字化けさせない
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    args = build_parser().parse_args(argv)
    if yaml is None:
        print(f"✖ PyYAML が必要です: {os.path.basename(sys.executable)} -m pip install pyyaml",
              file=sys.stderr)
        return 1
    if ACTIONS_DIR is None:
        print("✖ Composite Action のファイルが見つかりません。gh extension を再インストールしてください。",
              file=sys.stderr)
        return 1
    try:
        output, render = args.func(args)
    except QueryError as e:
        print(f"✖ {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(render(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
