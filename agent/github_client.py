"""GitHub API wrapper for pull request review operations.

What this module does:
It provides a small interface for the exact GitHub operations this project
needs: fetching a pull request diff, reading the head commit SHA, and posting
inline review comments.

Why it is structured this way:
The review agent should orchestrate business logic, not carry around GitHub
transport details. This wrapper keeps the integration narrow and readable.

How the key mechanism works:
Inline comments are posted through GitHub's review API using `line` and `side`
for more stable placement. We still parse diff positions for analysis, but when
it is time to post comments, we anchor them to concrete file lines on the RIGHT
side of the diff whenever possible.
"""

from __future__ import annotations

import json
from typing import Iterable, List
from urllib import request

from github import Github


class GitHubReviewClient:
    """Wrap pull request interactions used by the review bot."""

    def __init__(self, token: str) -> None:
        """Create a GitHub client authenticated with a token."""

        self.token = token
        self.github = Github(token)

    def get_pr_diff(self, repo_name: str, pr_number: int) -> str:
        """Return the raw unified diff for a pull request."""

        pull_request = self.github.get_repo(repo_name).get_pull(pr_number)
        diff_request = request.Request(
            pull_request.diff_url,
            headers={
                "Accept": "application/vnd.github.v3.diff",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "ai-code-review-bot",
            },
        )
        with request.urlopen(diff_request) as response:
            return response.read().decode("utf-8")

    def get_pr_head_sha(self, repo_name: str, pr_number: int) -> str:
        """Return the current head SHA for a pull request."""

        pull_request = self.github.get_repo(repo_name).get_pull(pr_number)
        return pull_request.head.sha

    def post_review_comments(
        self,
        repo_name: str,
        pr_number: int,
        commit_sha: str,
        comments: Iterable[dict],
    ) -> None:
        """Post up to 10 inline review comments to a pull request."""

        bounded_comments: List[dict] = list(comments)[:10]
        if not bounded_comments:
            return

        review_request = request.Request(
            f"https://api.github.com/repos/{repo_name}/pulls/{pr_number}/reviews",
            data=json.dumps(
                {
                    "commit_id": commit_sha,
                    "body": "Automated AI review suggestions.",
                    "event": "COMMENT",
                    "comments": bounded_comments,
                }
            ).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "ai-code-review-bot",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with request.urlopen(review_request):
            return
