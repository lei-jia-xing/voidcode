"""Lexical prescreen terms and judge candidates for ``find``."""

from __future__ import annotations

import re
from dataclasses import dataclass

from voidcode.capabilities.files.find_queries import FindGrepMatch

from .models import _MAX_KEYWORDS, _MAX_SNIPPET_MATCHES_PER_FILE, SkippedMatch


@dataclass
class _Candidate:
    candidate_id: int
    path: str
    match_lines: list[int]
    snippets: list[tuple[int, str]]


_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "by",
        "can",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "me",
        "of",
        "on",
        "or",
        "show",
        "than",
        "that",
        "the",
        "there",
        "these",
        "this",
        "those",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "with",
        "you",
        "find",
        "all",
    }
)


def extract_keywords(query: str) -> tuple[list[str], list[str]]:
    """Split a behavior description into lexical prescreen terms plus dropped terms."""
    seen: set[str] = set()
    kept: list[str] = []
    dropped: list[str] = []
    for token in re.findall(r"[A-Za-z0-9_]+", query.lower()):
        if len(token) < 2 or token in _STOPWORDS or token in seen:
            continue
        seen.add(token)
        if len(kept) < _MAX_KEYWORDS:
            kept.append(token)
        else:
            dropped.append(token)
    return kept, dropped


def _build_candidates(matches: list[tuple[str, FindGrepMatch]], *, max_candidates: int) -> tuple[list[_Candidate], list[SkippedMatch]]:
    by_file: dict[str, _Candidate] = {}
    order: list[str] = []
    for _, match in matches:
        candidate = by_file.get(match.file)
        if candidate is None:
            candidate = _Candidate(candidate_id=len(order), path=match.file, match_lines=[], snippets=[])
            by_file[match.file] = candidate
            order.append(match.file)
        if match.line not in candidate.match_lines:
            candidate.match_lines.append(match.line)
            if len(candidate.snippets) < _MAX_SNIPPET_MATCHES_PER_FILE:
                candidate.snippets.append((match.line, match.text))
    candidates = [by_file[path] for path in order]
    for candidate in candidates:
        candidate.match_lines.sort()
        candidate.snippets.sort(key=lambda item: item[0])
    skipped: list[SkippedMatch] = [
        {"path": candidate.path, "reason": "candidate_cap", "matches": len(candidate.match_lines)} for candidate in candidates[max_candidates:]
    ]
    return candidates[:max_candidates], skipped
