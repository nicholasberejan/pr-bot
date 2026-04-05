"""Main orchestration script for the AI code review bot.

What this module does:
It coordinates the full review loop from environment loading to OpenAI analysis
to GitHub review comment creation.

Why it is structured this way:
The design is intentionally linear so the control flow stays easy to inspect:
configuration, diff retrieval, parsing, chunking, model calls, validation, and
comment posting.

How the key mechanisms work:
- Team rules are loaded from YAML so policy stays outside the code.
- Large diffs are chunked to manage context size and preserve signal quality.
- The OpenAI call uses `response_format={"type": "json_object"}` so the model
  returns machine-readable output more reliably than prompt instructions alone.
"""

from __future__ import annotations

import json
from json import JSONDecodeError
import os
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import yaml
from openai import OpenAI

from diff_parser import DiffHunk, parse_unified_diff
from github_client import GitHubReviewClient


ACTION_ROOT = Path(os.getenv("ACTION_PATH", Path(__file__).resolve().parent.parent))
DEFAULT_RULES_PATH = ACTION_ROOT / "config" / "team_rules.yml"
MAX_DIFF_LINES_PER_CHUNK = 200
MAX_MODEL_TOKENS = 4096
MODEL_NAME = "gpt-4o"


def read_required_env(name: str) -> str:
    """Return a required environment variable or raise a helpful error."""

    value = os.getenv(name)
    if value is None:
        raise EnvironmentError(f"Missing required environment variable: {name}")
    normalized = value.strip()
    if not normalized:
        raise EnvironmentError(f"Missing required environment variable: {name}")
    return normalized


def load_team_rules(rules_path: Path) -> List[str]:
    """Load plain-English team review rules from YAML."""

    resolved_path = rules_path if rules_path.exists() else DEFAULT_RULES_PATH

    with resolved_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    rules = data.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
        raise ValueError("team_rules.yml must contain a top-level 'rules' list of strings.")
    return rules


def chunk_hunks(hunks: Sequence[DiffHunk], max_lines: int = MAX_DIFF_LINES_PER_CHUNK) -> List[List[DiffHunk]]:
    """Group hunks into chunks small enough for reliable LLM review."""

    chunks: List[List[DiffHunk]] = []
    current_chunk: List[DiffHunk] = []
    current_size = 0

    for hunk in hunks:
        hunk_size = len(hunk.lines)

        if current_chunk and current_size + hunk_size > max_lines:
            chunks.append(current_chunk)
            current_chunk = []
            current_size = 0

        current_chunk.append(hunk)
        current_size += hunk_size

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def build_prompt(rules: Sequence[str], hunks: Sequence[DiffHunk], anchor_map: Dict[str, dict]) -> str:
    """Create the user prompt sent to the OpenAI model for one diff chunk."""

    rendered_rules = "\n".join(f"- {rule}" for rule in rules)
    rendered_diff = render_hunks_with_anchors(hunks, anchor_map)

    return f"""Review the following pull request diff chunk.

Apply these team rules:
{rendered_rules}

Instructions:
- Return only concise, actionable review comments.
- Do not praise the code.
- Do not invent issues that are not grounded in the diff.
- Use only the provided `anchor_id` values when placing comments.
- Choose the single best `anchor_id` for the issue. Do not approximate.
- Prefer anchors on added lines. Use context-line anchors only when necessary.
- Return a JSON object with a single key named `comments`.
- Each item in `comments` must be an object with `path`, `anchor_id`, and `body`.
- If there are no issues, return {{"comments": []}}.

Diff chunk:
{rendered_diff}
"""


def normalize_json_payload(payload_text: str) -> Dict:
    """Parse model JSON output with a small amount of defensive cleanup."""

    cleaned = payload_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.replace("json", "", 1).strip()
    return json.loads(cleaned)


def extract_comments_from_response(payload: Dict) -> List[dict]:
    """Validate and normalize the model response into anchor-based comment dicts."""

    raw_comments = payload.get("comments", [])
    if not isinstance(raw_comments, list):
        raise ValueError("Model response must contain a 'comments' list.")

    comments: List[dict] = []
    for item in raw_comments:
        if not isinstance(item, dict):
            continue

        path = item.get("path")
        anchor_id = item.get("anchor_id")
        body = item.get("body")

        if not isinstance(path, str) or not path:
            continue
        if not isinstance(anchor_id, str) or not anchor_id.strip():
            continue
        if not isinstance(body, str) or not body.strip():
            continue

        comments.append(
            {
                "path": path,
                "anchor_id": anchor_id.strip(),
                "body": body.strip(),
            }
        )

    return comments


def build_anchor_map(hunks: Sequence[DiffHunk]) -> Dict[str, dict]:
    """Build stable anchor ids for commentable lines in a chunk."""

    anchor_map: Dict[str, dict] = {}
    anchor_number = 1

    for hunk in hunks:
        for line in hunk.lines:
            if line.new_line_number is None:
                continue

            anchor_id = f"A{anchor_number}"
            anchor_map[anchor_id] = {
                "path": hunk.filename,
                "line": line.new_line_number,
                "side": "RIGHT",
                "line_type": line.line_type,
                "content": line.content,
            }
            anchor_number += 1

    return anchor_map


def render_hunks_with_anchors(hunks: Sequence[DiffHunk], anchor_map: Dict[str, dict]) -> str:
    """Render diff hunks with explicit anchor ids for every commentable line."""

    anchor_lookup: Dict[tuple[str, int], str] = {}
    for anchor_id, metadata in anchor_map.items():
        anchor_lookup[(metadata["path"], metadata["line"])] = anchor_id

    rendered_chunks: List[str] = []
    current_file = None

    for hunk in hunks:
        if hunk.filename != current_file:
            current_file = hunk.filename
            rendered_chunks.append(f"FILE: {hunk.filename}")

        rendered_chunks.append(f"HUNK: {hunk.header}")
        for line in hunk.lines:
            old_line = line.old_line_number if line.old_line_number is not None else "null"
            new_line = line.new_line_number if line.new_line_number is not None else "null"
            anchor_id = ""
            if line.new_line_number is not None:
                anchor_id = anchor_lookup.get((hunk.filename, line.new_line_number), "")
            rendered_chunks.append(
                f"[anchor_id={anchor_id}][type={line.line_type}]"
                f"[old_line={old_line}][new_line={new_line}] {line.content}"
            )

    return "\n".join(rendered_chunks)


def resolve_comment_anchors(comments: Iterable[dict], anchor_map: Dict[str, dict]) -> List[dict]:
    """Convert model-selected anchor ids into GitHub review comment payloads."""

    resolved: List[dict] = []
    for comment in comments:
        anchor = anchor_map.get(comment["anchor_id"])
        if anchor is None or anchor["path"] != comment["path"]:
            continue

        resolved.append(
            {
                "path": anchor["path"],
                "line": anchor["line"],
                "side": anchor["side"],
                "body": comment["body"],
            }
        )

    return resolved


def valid_comment_targets(hunks: Iterable[DiffHunk]) -> set[tuple[str, int]]:
    """Return the set of `(path, new_line)` pairs that can accept RIGHT-side comments."""

    targets: set[tuple[str, int]] = set()
    for hunk in hunks:
        for line in hunk.lines:
            if line.new_line_number is not None:
                targets.add((hunk.filename, line.new_line_number))
    return targets


def filter_valid_comments(comments: Iterable[dict], hunks: Iterable[DiffHunk]) -> List[dict]:
    """Drop comments that do not point at valid new-file line numbers."""

    valid_targets = valid_comment_targets(hunks)
    filtered: List[dict] = []
    seen: set[tuple[str, int, str]] = set()

    for comment in comments:
        key = (comment["path"], comment["line"], comment["body"])
        target = (comment["path"], comment["line"])
        if target not in valid_targets or key in seen:
            continue
        filtered.append(comment)
        seen.add(key)

    return filtered


def review_diff_chunk(client: OpenAI, rules: Sequence[str], hunks: Sequence[DiffHunk]) -> List[dict]:
    """Send one diff chunk to the model and return normalized review comments."""

    system_prompt = (
        "You are a senior code reviewer. Focus on correctness, security, "
        "maintainability, and actionable feedback. Respond with valid JSON only."
    )
    anchor_map = build_anchor_map(hunks)
    user_prompt = build_prompt(rules, hunks, anchor_map)

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        max_tokens=MAX_MODEL_TOKENS,
    )

    payload_text = response.choices[0].message.content or '{"comments": []}'

    try:
        payload = normalize_json_payload(payload_text)
        comments = extract_comments_from_response(payload)
    except (JSONDecodeError, ValueError):
        return []

    resolved_comments = resolve_comment_anchors(comments, anchor_map)
    return filter_valid_comments(resolved_comments, hunks)


def main() -> None:
    """Run the end-to-end pull request review flow."""

    repo_name = read_required_env("REPO_NAME")
    pr_number = int(read_required_env("PR_NUMBER"))
    github_token = read_required_env("GITHUB_TOKEN")
    openai_api_key = read_required_env("OPENAI_API_KEY")
    rules_path = Path(os.getenv("RULES_FILE", str(DEFAULT_RULES_PATH)))

    rules = load_team_rules(rules_path)
    github_client = GitHubReviewClient(github_token)
    openai_client = OpenAI(api_key=openai_api_key)

    diff_text = github_client.get_pr_diff(repo_name, pr_number)
    hunks = parse_unified_diff(diff_text)
    if not hunks:
        return

    total_diff_lines = sum(len(hunk.lines) for hunk in hunks)
    review_chunks = chunk_hunks(hunks) if total_diff_lines > MAX_DIFF_LINES_PER_CHUNK else [hunks]

    all_comments: List[dict] = []
    for chunk in review_chunks:
        all_comments.extend(review_diff_chunk(openai_client, rules, chunk))

    final_comments = filter_valid_comments(all_comments, hunks)
    commit_sha = github_client.get_pr_head_sha(repo_name, pr_number)
    github_client.post_review_comments(repo_name, pr_number, commit_sha, final_comments)


if __name__ == "__main__":
    main()
