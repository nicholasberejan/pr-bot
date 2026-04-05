"""Parse unified diffs into hunks with GitHub review positions.

What this module does:
It converts GitHub's raw unified diff text into structured hunks that preserve
file paths, hunk boundaries, and per-line metadata.

Why it is structured this way:
The rest of the system should not need to understand diff syntax directly.
By centralizing parsing here, the review agent can focus on orchestration and
the GitHub client can focus on transport.

How the key mechanism works:
GitHub inline review comments use a diff `position`, not just a source line
number. This parser computes that position for every line in each file's diff
so the LLM can reference valid comment targets.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import List, Optional


HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


@dataclass
class DiffLine:
    """Represent a single line inside a diff hunk."""

    content: str
    line_type: str
    position: int
    old_line_number: Optional[int]
    new_line_number: Optional[int]


@dataclass
class DiffHunk:
    """Represent one unified diff hunk for a file."""

    filename: str
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: List[DiffLine]


def parse_hunk_header(header: str) -> tuple[int, int, int, int]:
    """Extract old and new line ranges from a unified diff hunk header."""

    match = HUNK_HEADER_RE.match(header)
    if not match:
        raise ValueError(f"Invalid hunk header: {header}")

    old_start = int(match.group("old_start"))
    old_count = int(match.group("old_count") or "1")
    new_start = int(match.group("new_start"))
    new_count = int(match.group("new_count") or "1")
    return old_start, old_count, new_start, new_count


def parse_unified_diff(diff_text: str) -> List[DiffHunk]:
    """Parse a unified diff string into a list of structured hunks.

    The parser tracks positions at the file level because GitHub review comment
    positions are counted down the diff body for a single file, across hunks.
    Hunk headers themselves are not assigned comment positions.
    """

    hunks: List[DiffHunk] = []
    current_file: Optional[str] = None
    current_hunk: Optional[DiffHunk] = None
    file_position = 0
    old_line = 0
    new_line = 0

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("diff --git "):
            if current_hunk is not None:
                hunks.append(current_hunk)
                current_hunk = None
            current_file = None
            file_position = 0
            continue

        if raw_line.startswith("+++ b/"):
            current_file = raw_line[6:]
            continue

        if raw_line.startswith("@@ "):
            if current_file is None:
                continue

            if current_hunk is not None:
                hunks.append(current_hunk)

            old_start, old_count, new_start, new_count = parse_hunk_header(raw_line)
            current_hunk = DiffHunk(
                filename=current_file,
                header=raw_line,
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=[],
            )
            old_line = old_start
            new_line = new_start
            continue

        if current_hunk is None:
            continue

        if raw_line.startswith("\\ No newline at end of file"):
            continue

        prefix = raw_line[:1]
        if prefix not in {" ", "+", "-"}:
            continue

        file_position += 1
        old_number: Optional[int]
        new_number: Optional[int]

        if prefix == " ":
            old_number = old_line
            new_number = new_line
            old_line += 1
            new_line += 1
            line_type = "context"
        elif prefix == "+":
            old_number = None
            new_number = new_line
            new_line += 1
            line_type = "addition"
        else:
            old_number = old_line
            new_number = None
            old_line += 1
            line_type = "deletion"

        current_hunk.lines.append(
            DiffLine(
                content=raw_line[1:],
                line_type=line_type,
                position=file_position,
                old_line_number=old_number,
                new_line_number=new_number,
            )
        )

    if current_hunk is not None:
        hunks.append(current_hunk)

    return hunks


def render_hunks_for_prompt(hunks: List[DiffHunk]) -> str:
    """Render structured hunks into a compact prompt-friendly string."""

    rendered_chunks: List[str] = []
    current_file: Optional[str] = None

    for hunk in hunks:
        if hunk.filename != current_file:
            current_file = hunk.filename
            rendered_chunks.append(f"FILE: {hunk.filename}")

        rendered_chunks.append(f"HUNK: {hunk.header}")
        for line in hunk.lines:
            old_line = line.old_line_number if line.old_line_number is not None else "null"
            new_line = line.new_line_number if line.new_line_number is not None else "null"
            rendered_chunks.append(
                f"[position={line.position}][type={line.line_type}]"
                f"[old_line={old_line}][new_line={new_line}] {line.content}"
            )

    return "\n".join(rendered_chunks)
