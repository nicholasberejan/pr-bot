"""GitHub API wrapper for pull request review operations.

What this module does:
It provides a small interface for the exact GitHub operations this project
needs: fetching a pull request diff, reading the head commit SHA, and posting
inline review comments.

Why it is structured this way:
The review agent should orchestrate business logic, not carry around GitHub
transport details. This wrapper keeps the integration narrow and readable.

How the key mechanism works:
Inline comments are posted through GitHub's review API with a diff `position`.
That position is not a file line number; it is the line's offset inside the PR
diff for that file. The parser computes those positions, and this client sends
them back to GitHub unchanged.
"""

from __future__ import annotations

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

        repository = self.github.get_repo(repo_name)
        pull_request = repository.get_pull(pr_number)
        commit = repository.get_commit(commit_sha)
        pull_request.create_review(
            commit=commit,
            body="Automated AI review suggestions.",
            event="COMMENT",
            comments=bounded_comments,
        )
