# AI Code Review Bot

This project is a publishable custom GitHub Action that reviews pull requests with the OpenAI API and posts inline comments back to GitHub.

## What This Project Does

When a pull request is opened or updated, a workflow in the consuming repository runs this action, and the action:

1. Fetches the pull request diff from GitHub.
2. Parses the unified diff into hunks with file paths and diff positions.
3. Loads team-specific review rules from [`config/team_rules.yml`](/Users/njberejan/Projects/personal/pr-bot/config/team_rules.yml).
4. Sends the diff to `gpt-4o` in manageable chunks.
5. Receives structured review comments as JSON.
6. Posts those comments back to the pull request using the GitHub Review API.

## Architecture

```text
+-------------------+
| Pull Request Event|
+---------+---------+
          |
          v
+-----------------------------+
| Consumer Repo Workflow      |
| uses: owner/repo@v1         |
+--------------+--------------+
               |
               v
+-----------------------------+
| action.yml                  |
| Packages the Python bot     |
+--------------+--------------+
               |
               v
+-----------------------------+
| review_agent.py             |
| Orchestrates the flow       |
+-------+-----------+---------+
        |           |
        |           v
        |      +------------------+
        |      | github_client.py |
        |      +------------------+
        |
        v
+-----------------------------+
| diff_parser.py              |
| Unified diff -> hunks       |
+--------------+--------------+
               |
               v
+-----------------------------+
| OpenAI API (`gpt-4o`)       |
| Structured JSON comments    |
+--------------+--------------+
               |
               v
+-----------------------------+
| GitHub Review API           |
| Inline PR review comments   |
+-----------------------------+
```

## File-By-File Walkthrough

### [`action.yml`](/Users/njberejan/Projects/personal/pr-bot/action.yml)

What:
This file turns the repository into a reusable custom GitHub Action.

Why:
Without `action.yml`, this repo is just source code. `action.yml` is the contract that tells GitHub which inputs the action accepts and how to execute it.

How:
This action is implemented as a composite action. It installs Python, installs dependencies from this action repository, and runs [`agent/review_agent.py`](/Users/njberejan/Projects/personal/pr-bot/agent/review_agent.py).

Why composite action instead of Docker or JavaScript:

- Composite keeps the packaging simple and readable.
- You can see the exact execution steps.
- It works well for a Python-based tool without introducing container build complexity.

### [`.github/workflows/code-review.yml`](/Users/njberejan/Projects/personal/pr-bot/.github/workflows/code-review.yml)

What:
This workflow is now an example consumer workflow. It shows how a repository can invoke the custom action.

Why:
When you publish this action, other repositories will use a workflow that looks like this one, except their `uses:` line will point to `your-username/your-repo@v1` instead of `./`.

How:
The workflow listens for `pull_request` events of type `opened` and `synchronize`, checks out the consuming repository, and calls the action through `uses:`.

Why these environment variables matter:

- `OPENAI_API_KEY`: authenticates requests to the OpenAI API.
- `GITHUB_TOKEN`: authenticates calls back to GitHub so the bot can read PR data and create review comments.
- `PR_NUMBER`: tells the script which pull request triggered the run.
- `REPO_NAME`: tells the script which repository to query in `owner/repo` format.
- `RULES_FILE`: points to the team rules file in the consuming repository workspace.

How GitHub secrets work:
Secrets are encrypted values stored in the repository or organization settings. In the workflow, we reference them through `${{ secrets.NAME }}` so the values are injected at runtime without being committed to source control.

### [`config/team_rules.yml`](/Users/njberejan/Projects/personal/pr-bot/config/team_rules.yml)

What:
This file defines the review policy in plain English.

Why:
Externalizing rules keeps policy separate from code. Teams can update review criteria without touching Python logic, redeploying infrastructure, or editing prompt strings in the codebase.

How:
The agent loads the YAML file at runtime, formats the rules into the prompt, and applies the same orchestration logic to whatever rules the team chooses. When this action is used from another repository, that repository can provide its own rules file path as an action input.

This is a strong architectural choice because it makes the system:

- Easier to customize for different teams.
- Safer to evolve without changing code paths.
- Better for experimentation because prompt policy can change independently of transport logic.

### [`agent/diff_parser.py`](/Users/njberejan/Projects/personal/pr-bot/agent/diff_parser.py)

What:
This module turns a raw unified diff string into structured hunks with file paths, hunk headers, and per-line metadata.

Why:
The LLM needs more than a raw blob of text. To produce usable inline comments, we need a reliable mapping from the visible diff lines to GitHub's review `position` field.

How unified diffs work:

- A file section starts with lines like `diff --git a/file.py b/file.py`.
- Hunk headers look like `@@ -10,4 +10,6 @@`.
- `-10,4` means the old file hunk starts at line 10 and spans 4 lines.
- `+10,6` means the new file hunk starts at line 10 and spans 6 lines.
- Lines beginning with `-` are deletions, `+` are additions, and a leading space is context.

Why line mapping matters:
GitHub inline comments do not use plain source line numbers in this API path. They use a diff `position`, which is the line's location inside the pull request diff for that file. If that mapping is wrong, comments land on the wrong line or the API rejects them.

### [`agent/github_client.py`](/Users/njberejan/Projects/personal/pr-bot/agent/github_client.py)

What:
This is a thin wrapper around GitHub access patterns used by the agent.

Why:
Separating GitHub transport from orchestration keeps the main script easier to read and easier to test. The agent should decide what to say; this module should decide how to talk to GitHub.

How:
It fetches the PR diff, gets the head commit SHA, and posts a review with inline comments. Comments are capped at 10 per PR to keep feedback high-signal. Without a cap, LLM review bots can become noisy and reduce trust.

`position` vs. line number:
`position` refers to the line's place inside the rendered diff, not a raw source line. This is why the diff parser computes positions explicitly and the model is asked to reference those positions rather than guessing file line numbers.

### [`agent/review_agent.py`](/Users/njberejan/Projects/personal/pr-bot/agent/review_agent.py)

What:
This is the main entry point that orchestrates the full review loop.

Why:
Keeping orchestration in one readable script makes the control flow easy to learn: load config, fetch diff, parse, chunk, call model, validate output, post review.

How:
It chunks large diffs when they exceed 200 diff lines, sends each chunk with the rules and diff metadata to `gpt-4o`, validates the model output, and posts the combined comments back to GitHub. It also supports two rule sources:

- a consumer-repository rules file passed in through the action input
- the bundled default rules file shipped with this action repo

Structured outputs from LLMs:
This project uses `response_format={"type": "json_object"}` when calling the OpenAI API. That matters because prompt-only instructions like "respond in JSON" are a soft request. `response_format` adds an API-level constraint so the model is much more likely to return machine-parseable output.

One subtle point:
The `json_object` response format requires a top-level JSON object, so the agent asks the model to return:

```json
{
  "comments": [
    {
      "path": "agent/review_agent.py",
      "position": 12,
      "body": "Use a narrower exception here."
    }
  ]
}
```

That is slightly different from a bare JSON list, but it is more reliable with the API contract. The parser still handles edge cases defensively in case the model returns fenced JSON or malformed content.

Direct API calls vs. function calling:
This project uses direct Python API calls to GitHub rather than giving the model tools. That keeps authority in deterministic code:

- Python decides what to fetch and what to post.
- The model only performs analysis.
- Failures are easier to debug because API calls are not hidden inside model planning.

You would choose function calling when the model needs to decide dynamically which tool to use next. For this project, the workflow is linear and predictable, so direct orchestration is simpler and safer.

Context window management:
Large pull requests can overflow the model context window or dilute attention. Chunking keeps each request focused while preserving the critical metadata the model needs:

- file path
- diff `position`
- hunk structure
- team rules

Agentic orchestration:
This bot is a simple linear agent. To make it more dynamic, you could let the model choose between tools such as:

- fetch more file context
- retrieve coding standards
- draft summary comments
- decide whether to post inline comments or only a top-level review

That would move the system toward function calling or an assistant-style multi-step loop. For learning fundamentals, the current design is intentionally explicit and deterministic.

## Publish And Use This Action

### 1. Publish this repository

Push this repository to GitHub as its own repository. If you want other GitHub users to be able to use it, make the repository public.

### 2. Tag a release

After pushing, create a tag so consumers can reference a stable version:

```bash
git tag v1
git push origin v1
```

Later, you can move `v1` to newer compatible releases or create more specific tags like `v1.0.0`.

### 3. Add a workflow to any repository that should use the bot

In the consuming repository, create [`.github/workflows/code-review.yml`](/Users/njberejan/Projects/personal/pr-bot/.github/workflows/code-review.yml)-style automation like this:

```yaml
name: AI Code Review

on:
  pull_request:
    types:
      - opened
      - synchronize

jobs:
  review:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      pull-requests: write

    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Run AI review
        uses: your-username/your-action-repo@v1
        with:
          openai_api_key: ${{ secrets.OPENAI_API_KEY }}
          github_token: ${{ secrets.GITHUB_TOKEN }}
          pr_number: ${{ github.event.pull_request.number }}
          repo_name: ${{ github.repository }}
          rules_file: .github/ai-review/team_rules.yml
```

This is the key architectural difference from the earlier repo-local version:

- the action code lives in one repository
- the workflow lives in each repository you want reviewed
- the workflow passes repository-specific context into the shared action

### 4. Add repository secrets in each consuming repository

In every repository that uses the action:

1. Go to `Settings`
2. Open `Secrets and variables` -> `Actions`
3. Add `OPENAI_API_KEY`

`GITHUB_TOKEN` is still provided automatically by GitHub.

### 5. Add a rules file in the consuming repository

Create:

```text
.github/ai-review/team_rules.yml
```

Example:

```yaml
rules:
  - "No hardcoded secrets or API keys."
  - "Functions should have docstrings."
  - "Avoid deeply nested conditionals (more than 3 levels)."
  - "No print() statements in production code; use logging instead."
  - "Flag any TODO or FIXME comments."
```

This keeps team policy owned by the consuming repository instead of buried inside the shared action code.

## Local Development

### 1. Install dependencies locally

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Run locally

Set the same environment variables that the workflow provides:

```bash
export OPENAI_API_KEY="your-openai-key"
export GITHUB_TOKEN="your-github-token"
export REPO_NAME="owner/repo"
export PR_NUMBER="123"
export ACTION_PATH="$(pwd)"
python agent/review_agent.py
```

## Customizing Review Rules

For this action repository itself, edit [`config/team_rules.yml`](/Users/njberejan/Projects/personal/pr-bot/config/team_rules.yml).

For a consuming repository, create or edit its own `.github/ai-review/team_rules.yml` and point the workflow input at that file.

Example customizations:

- Add security-specific checks.
- Add framework-specific conventions.
- Tighten or loosen style preferences.
- Remove noisy rules that do not matter to your team.

Because the rules are loaded at runtime, changing that YAML file changes the bot's behavior without changing the orchestration code.
