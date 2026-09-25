"""Candidate discovery and evidence extraction for the Choice questions.

A Choice can only pick from the options it is given, so the *content* of each
option's description is the whole game. This module turns a filesystem path into
a short, self-describing evidence string, and enforces the candidate bounds that
`MAX_CHOICE_OPTIONS` alone does not cover.

Pure functions, no network, no module state: tests drive `build_criteria` with an
injected reader so nothing here needs a filesystem.
"""

import re
from pathlib import PurePosixPath
from typing import Callable, Dict, Iterable, List, Sequence, Tuple

from limits import (
    MAX_CANDIDATE_CHARS,
    MAX_PREVIEW_READ_CHARS,
    MAX_RANK_CANDIDATES,
    MAX_TOTAL_CRITERIA_CHARS,
    truncate_text,
)

#: Placeholder for a candidate whose bytes could not be read. Never guess content.
UNREADABLE = "<unreadable>"

#: A preview with more NUL than this ratio is binary noise, not evidence.
NUL_RATIO_LIMIT = 0.02

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_BLANK_RUN = re.compile(r"\n{3,}")
_BLOCK_MARKER = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*|>[ \t]?)", re.MULTILINE)
_TABLE_PIPE = re.compile(r"[ \t]*\|[ \t]*")
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)", re.MULTILINE)
_FOLDED = {">", ">-", ">+", "|", "|-", "|+"}


def front_matter_split(text: str) -> Tuple[str, str]:
    """Split YAML front matter (`---\\n…\\n---`) from the body.

    Returns ``(front_matter, body)``. A document without front matter yields
    ``("", text)``; a document that opens with `---` but never closes it is
    treated as having no front matter rather than swallowing the whole file.
    """
    if not text:
        return "", ""
    match = _FRONTMATTER.match(text)
    if not match:
        return "", text
    return match.group(1), text[match.end():]


def _collapse(text: str) -> str:
    """Drop comments and syntax noise, keeping the words a reader needs."""
    cleaned = _HTML_COMMENT.sub(" ", text)
    cleaned = _FENCE.sub("", cleaned)
    cleaned = _BLOCK_MARKER.sub("", cleaned)
    cleaned = _TABLE_PIPE.sub(" ", cleaned)
    cleaned = _BLANK_RUN.sub("\n\n", cleaned)
    return cleaned.strip()


def markdown_preview(text: str, max_chars: int = MAX_CANDIDATE_CHARS) -> str:
    """Short, self-describing evidence for a candidate file.

    Drops front matter and HTML comments, strips markdown syntax noise (heading
    markers, blockquote markers, table pipes, code fences), collapses blank-line
    runs, then truncates with an explicit marker.
    """
    if not text:
        return ""
    _, body = front_matter_split(text)
    return truncate_text(_collapse(body), max_chars)


def skill_description(text: str) -> str:
    """The `description:` field of a SKILL.md, or "" when there is none.

    That field is written to tell an agent when the skill applies, which is
    exactly the signal the Choice needs, and it is nearly free to extract. Both
    plain (`description: run the tests`) and folded/blocked YAML scalars are
    read. Without it, `candidate_preview` falls back to the document body.
    """
    front, _ = front_matter_split(text)
    if not front:
        return ""
    lines = front.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^description\s*:\s*(.*)$", line)
        if not match:
            continue
        value = match.group(1).strip()
        if value in _FOLDED:
            parts: List[str] = []
            for following in lines[index + 1:]:
                if following.strip() and not following[:1].isspace():
                    break
                parts.append(following.strip())
            value = " ".join(p for p in parts if p)
        value = value.strip().strip("\"'").strip()
        return " ".join(value.split())
    return ""


def looks_binary(text: str) -> bool:
    """True when a document carries enough NUL to be binary noise."""
    if not text:
        return False
    return text.count("\x00") / len(text) > NUL_RATIO_LIMIT


def read_head(path, max_chars: int = MAX_PREVIEW_READ_CHARS) -> str:
    """Read at most `max_chars` from the head of a file. Never raises."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read(max_chars)
    except Exception:
        return ""


def candidate_preview(path: str, text: str, max_chars: int = MAX_CANDIDATE_CHARS) -> str:
    """Evidence for one candidate: front-matter description, else a markdown preview."""
    if not text or looks_binary(text):
        return ""
    if PurePosixPath(path).name.lower() == "skill.md":
        described = skill_description(text)
        if described:
            return truncate_text(described, max_chars)
    return markdown_preview(text, max_chars)


def build_criteria(
    paths: Sequence[str],
    read_text: Callable[[str], str],
    max_chars: int = MAX_CANDIDATE_CHARS,
    total_chars: int = MAX_TOTAL_CRITERIA_CHARS,
) -> Dict[str, str]:
    """Map `path -> evidence description`, one entry per candidate.

    The per-candidate share is `total_chars / len(paths)`, so a 250-candidate
    request cannot blow the token budget just because every file is large.
    Never raises: an unreadable candidate yields `UNREADABLE` rather than
    removing the option, because a Choice cannot pick a value it was not given.
    """
    items = list(paths)
    allowance = max(64, min(max_chars, total_chars // max(len(items), 1)))
    criteria: Dict[str, str] = {}
    for path in items:
        try:
            raw = read_text(path) or ""
        except Exception:
            raw = ""
        preview = candidate_preview(path, raw, allowance) if raw.strip() else ""
        criteria[path] = preview or UNREADABLE
    return criteria


def bound_candidates(
    candidates: Iterable[str], limit: int = MAX_RANK_CANDIDATES
) -> Tuple[List[str], bool]:
    """Return `(kept, truncated)` for a candidate list.

    `truncated` is the signal a caller must surface: a candidate dropped here
    was never offered to the model, so the result may be confidently wrong.
    """
    items = list(candidates)
    if not limit or limit <= 0 or len(items) <= limit:
        return items, False
    return items[:limit], True


def read_text_cache(root=None) -> Tuple[Dict[str, str], Callable[[str], str]]:
    """A `(cache, reader)` pair: read each path once, reuse everywhere.

    `jev_engine` needs the same bytes twice — once to build a 2k preview for the
    Choice criteria and once to return up to `MAX_CONTENT_CHARS` as the
    resource content. Reading twice is pure waste. The head limit is above
    `MAX_CONTENT_CHARS` so one read stays honest for both consumers. `root`
    resolves workspace-relative candidate paths; without it the paths are used
    as given.
    """
    cache: Dict[str, str] = {}

    def read_text(path: str) -> str:
        if path not in cache:
            target = f"{root}/{path}" if root else path
            cache[path] = read_head(target)
        return cache[path]

    return cache, read_text
