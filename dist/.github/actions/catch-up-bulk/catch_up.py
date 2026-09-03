#!/usr/bin/env python3
"""過去のマージ済みPRを一括でLLM抽出→データ記録するキャッチアップエンジン。"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

import yaml

actions_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(actions_dir, "shared"))
sys.path.insert(0, os.path.join(actions_dir, "extract-components"))
sys.path.insert(0, os.path.join(actions_dir, "record-mapping"))

from llm_utils import detect_provider, call_llm, parse_llm_response
from extract_components import (
    format_components_for_prompt,
    build_prompt,
    validate_component_ids,
)
from update_data import update_mappings, update_timeline, read_model_version


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="キャッチアップ: 過去PRの一括LLM抽出")
    parser.add_argument("--repo", required=True, help="OWNER/REPO")
    parser.add_argument("--data-branch", default="_architecture-tracker")
    parser.add_argument("--work-dir", required=True, help="データブランチのクローン先")
    parser.add_argument("--prompt-template", required=True, help="prompt.txtのパス")
    parser.add_argument("--provider", help="LLMプロバイダー")
    parser.add_argument("--model", default=None)
    parser.add_argument("--since", default=None, help="開始日 (ISO 8601)")
    parser.add_argument("--until", default=None, help="終了日 (ISO 8601)")
    parser.add_argument("--pr-numbers", default=None, help="対象PR番号 (カンマ区切り)")
    parser.add_argument("--source-repo", default=None, help="外部リポジトリ (OWNER/REPO)")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def fetch_merged_prs(repo, since=None, until=None, pr_numbers=None):
    """GitHub APIからマージ済みPRを取得する。"""
    if pr_numbers:
        prs = []
        for num in pr_numbers:
            result = subprocess.run(
                ["gh", "api", f"repos/{repo}/pulls/{num}"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                print(f"Warning: PR #{num} の取得に失敗: {result.stderr.strip()}", file=sys.stderr)
                continue
            pr = json.loads(result.stdout)
            if pr.get("merged_at"):
                prs.append(pr)
            else:
                print(f"Warning: PR #{num} は未マージ、スキップ", file=sys.stderr)
        return prs

    prs = []
    page = 1
    per_page = 100
    while True:
        cmd = [
            "gh", "api",
            f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page={per_page}&page={page}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error: PR一覧の取得に失敗: {result.stderr.strip()}", file=sys.stderr)
            break
        page_prs = json.loads(result.stdout)
        if not page_prs:
            break

        for pr in page_prs:
            merged_at = pr.get("merged_at")
            if not merged_at:
                continue
            if since and merged_at < since:
                continue
            if until and merged_at > until:
                continue
            prs.append(pr)

        if since:
            oldest_updated = page_prs[-1].get("updated_at", "")
            if oldest_updated < since:
                break

        if len(page_prs) < per_page:
            break
        page += 1

    return prs


def filter_already_recorded(prs, mappings_data):
    """既にmappings.jsonに記録済みのPRを除外する。"""
    existing = set(mappings_data.get("mappings", {}).keys())
    filtered = []
    skipped = []
    for pr in prs:
        if str(pr["number"]) in existing:
            skipped.append(pr["number"])
        else:
            filtered.append(pr)
    return filtered, skipped


def extract_for_pr(pr, components_data, prompt_template, provider, model=None):
    """1つのPRに対してLLM抽出を実行する。"""
    pr_description = pr.get("body") or ""
    if not pr_description.strip():
        return [], "PR descriptionが空のため抽出スキップ"

    components_text = format_components_for_prompt(components_data)
    prompt = build_prompt(prompt_template, components_text, pr_description)

    raw_response = call_llm(provider, prompt, model, max_tokens=1024)
    parsed = parse_llm_response(raw_response)

    raw_components = parsed.get("affected_components", [])
    if isinstance(raw_components, str):
        raw_components = [raw_components]
    components = validate_component_ids(raw_components, components_data)
    reasoning = parsed.get("reasoning", "")
    return components, reasoning


def extract_diff_stats_from_pr(pr_data):
    """PRデータからdiff統計とラベルを抽出する（API呼び出しなし）。"""
    try:
        diff_stats = {
            "additions": pr_data["additions"],
            "deletions": pr_data["deletions"],
            "changed_files": pr_data["changed_files"],
        }
    except KeyError:
        diff_stats = None
    labels = [l["name"] for l in pr_data.get("labels", []) if isinstance(l, dict)]
    return diff_stats, labels if labels else None


def fetch_pr_diff_stats(repo, pr_number):
    """PRのdiff統計とラベルをAPIで取得する。"""
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/pulls/{pr_number}",
         "--jq", '{additions, deletions, changed_files, labels: [.labels[].name]}'],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None, None
    try:
        data = json.loads(result.stdout)
        diff_stats = {
            "additions": data["additions"],
            "deletions": data["deletions"],
            "changed_files": data["changed_files"],
        }
        labels = data.get("labels", [])
        return diff_stats, labels if labels else None
    except (json.JSONDecodeError, KeyError):
        return None, None


def git_commit_and_push(work_dir, data_branch, message, max_retries=3):
    """データブランチにコミット&プッシュする。push失敗時はpull --rebaseでリトライ。"""
    subprocess.run(
        ["git", "-C", work_dir, "add", "mappings.json", "timeline.json"],
        check=True,
    )

    result = subprocess.run(
        ["git", "-C", work_dir, "diff", "--cached", "--quiet"],
        capture_output=True,
    )
    if result.returncode == 0:
        return False

    subprocess.run(
        ["git", "-C", work_dir, "commit", "-m", message],
        check=True, capture_output=True,
    )

    for attempt in range(max_retries):
        push_result = subprocess.run(
            ["git", "-C", work_dir, "push", "origin", data_branch],
            capture_output=True, text=True,
        )
        if push_result.returncode == 0:
            return True

        if attempt < max_retries - 1:
            print(f"⚠️ Push failed (attempt {attempt + 1}/{max_retries}), pulling and retrying...", file=sys.stderr)
            pull_result = subprocess.run(
                ["git", "-C", work_dir, "pull", "--rebase", "origin", data_branch],
                capture_output=True, text=True,
            )
            if pull_result.returncode != 0:
                print(f"Warning: pull --rebase failed: {pull_result.stderr.strip()}", file=sys.stderr)
                return False
            time.sleep((attempt + 1) * 2)

    print(f"Warning: push failed after {max_retries} retries: {push_result.stderr.strip()}", file=sys.stderr)
    return False


def run(args):
    """メイン処理ループ。"""
    provider = args.provider or detect_provider()
    if provider is None and not args.dry_run:
        print("Error: LLMプロバイダーが設定されていません", file=sys.stderr)
        sys.exit(1)

    target_repo = args.source_repo or args.repo
    repo_pattern = re.compile(r"^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$")
    if not repo_pattern.match(target_repo):
        print(f"Error: 不正なリポジトリ形式: {target_repo}", file=sys.stderr)
        sys.exit(1)

    pr_numbers_list = None
    if args.pr_numbers:
        pr_numbers_list = []
        for n in args.pr_numbers.split(","):
            n = n.strip()
            if not n:
                continue
            try:
                pr_numbers_list.append(int(n))
            except ValueError:
                print(f"Warning: 無効なPR番号 '{n}' をスキップ", file=sys.stderr)

    mappings_path = os.path.join(args.work_dir, "mappings.json")
    timeline_path = os.path.join(args.work_dir, "timeline.json")
    components_path = os.path.join(args.work_dir, "components.yaml")

    with open(mappings_path, encoding="utf-8") as f:
        mappings_data = json.load(f)
    with open(timeline_path, encoding="utf-8") as f:
        timeline_data = json.load(f)
    with open(components_path, encoding="utf-8") as f:
        components_data = yaml.safe_load(f)

    with open(args.prompt_template, encoding="utf-8") as f:
        prompt_template = f.read()

    model_version = read_model_version(components_path)

    print(f"📋 マージ済みPR取得中... (repo: {target_repo})")
    prs = fetch_merged_prs(target_repo, args.since, args.until, pr_numbers_list)
    print(f"   取得PR数: {len(prs)}")

    if not prs:
        print("対象PRが0件です。")
        _set_outputs(0, 0, 0, 0)
        return

    prs_to_process, skipped_prs = filter_already_recorded(prs, mappings_data)
    print(f"   既記録スキップ: {len(skipped_prs)}件")
    print(f"   処理対象: {len(prs_to_process)}件")

    if not prs_to_process:
        print("全て記録済みです。")
        _set_outputs(0, 0, 0, len(skipped_prs))
        return

    prs_to_process.sort(key=lambda p: p.get("merged_at", ""))

    succeeded = 0
    failed = 0
    failed_details = []
    batch_count = 0

    for i, pr in enumerate(prs_to_process):
        pr_number = pr["number"]
        pr_title = pr.get("title", "")
        pr_url = pr.get("html_url", "")
        merged_at = pr.get("merged_at", "")
        author = pr.get("user", {}).get("login", "unknown")

        print(f"\n🔍 [{i+1}/{len(prs_to_process)}] PR #{pr_number}: {pr_title}")

        if args.dry_run:
            print(f"   [dry-run] スキップ")
            continue

        try:
            components, reasoning = extract_for_pr(
                pr, components_data, prompt_template, provider, args.model,
            )
            print(f"   コンポーネント: {components}")
            if reasoning:
                print(f"   理由: {reasoning}")

            diff_stats, labels = extract_diff_stats_from_pr(pr)
            if diff_stats is None:
                diff_stats, labels = fetch_pr_diff_stats(target_repo, pr_number)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

            mappings_data = update_mappings(
                mappings_data, pr_number, pr_title, pr_url, merged_at,
                components, author, now,
                source="ai", ai_components=components,
                model_version=model_version,
                diff_stats=diff_stats, labels=labels,
                auto_approved=True,
            )
            timeline_data = update_timeline(
                timeline_data, pr_number, pr_title, pr_url,
                components, author, now,
                source="ai", ai_components=components,
                diff_stats=diff_stats, labels=labels,
                auto_approved=True, merged_at=merged_at,
            )

            with open(mappings_path, "w", encoding="utf-8") as f:
                json.dump(mappings_data, f, indent=2, ensure_ascii=False)
                f.write("\n")
            with open(timeline_path, "w", encoding="utf-8") as f:
                json.dump(timeline_data, f, indent=2, ensure_ascii=False)
                f.write("\n")

            succeeded += 1
            batch_count += 1

            if batch_count >= args.batch_size:
                print(f"\n📦 バッチコミット ({batch_count}件)")
                pushed = git_commit_and_push(
                    args.work_dir, args.data_branch,
                    f"Catch-up bulk: {batch_count} PRs (up to PR #{pr_number})",
                )
                if pushed:
                    print("   ✅ プッシュ成功")
                else:
                    print("   ⚠️ プッシュ失敗、以降の処理を中断します", file=sys.stderr)
                    break
                batch_count = 0

        except Exception as e:
            print(f"   ❌ 失敗: {e}", file=sys.stderr)
            failed += 1
            failed_details.append({"pr_number": pr_number, "error": str(e)})

        if i < len(prs_to_process) - 1:
            time.sleep(1)

    if batch_count > 0 and not args.dry_run:
        try:
            print(f"\n📦 最終バッチコミット ({batch_count}件)")
            pushed = git_commit_and_push(
                args.work_dir, args.data_branch,
                f"Catch-up bulk: {batch_count} PRs (final batch)",
            )
            if pushed:
                print("   ✅ プッシュ成功")
            else:
                print("   ⚠️ 最終バッチのプッシュ失敗", file=sys.stderr)
        except Exception as e:
            print(f"   ❌ 最終バッチコミット失敗: {e}", file=sys.stderr)

    # Mermaid図はモデル全体の再生成なので最後のPR番号1回のディスパッチで十分。
    # バルクキャッチアップ対象の古いPRにはトラッキングIssueが存在しない想定。
    if succeeded > 0 and not args.dry_run:
        last_pr = prs_to_process[-1]["number"]
        _dispatch_post_record(args.repo, last_pr)

    print("\n" + "=" * 50)
    print(f"📊 サマリー")
    print(f"   処理対象: {len(prs_to_process)}件")
    print(f"   成功: {succeeded}件")
    print(f"   失敗: {failed}件")
    print(f"   スキップ(既記録): {len(skipped_prs)}件")
    if failed_details:
        print(f"\n❌ 失敗詳細:")
        for detail in failed_details:
            print(f"   PR #{detail['pr_number']}: {detail['error']}")

    _set_outputs(len(prs_to_process), succeeded, failed, len(skipped_prs))


def _dispatch_post_record(repo, pr_number):
    """post-recordイベントをディスパッチする。"""
    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/dispatches",
         "--method", "POST",
         "-f", "event_type=post-record",
         "-f", f"client_payload[pr_number]={pr_number}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"📤 post-record ディスパッチ完了 (PR #{pr_number})")
    else:
        print(f"Warning: post-record ディスパッチ失敗: {result.stderr.strip()}", file=sys.stderr)


def _set_outputs(processed, succeeded, failed, skipped):
    """GitHub Actions の出力を設定する。"""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(f"processed={processed}\n")
            f.write(f"succeeded={succeeded}\n")
            f.write(f"failed={failed}\n")
            f.write(f"skipped={skipped}\n")


def main():
    args = parse_args()
    run(args)


if __name__ == "__main__":
    main()
