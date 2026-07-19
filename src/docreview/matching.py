from __future__ import annotations

import re
import unicodedata

from .models import BlockMatch, KeywordRule


def parse_keyword_text(raw: str) -> list[KeywordRule]:
    rules: list[KeywordRule] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        mode = "regex" if line.startswith("re:") else "literal"
        value = line[3:].strip() if mode == "regex" else line
        if not value:
            continue
        key = (mode, value)
        if key not in seen:
            seen.add(key)
            rules.append(KeywordRule(value=value, mode=mode))
    return rules


def find_matches(text: str, rules: list[KeywordRule]) -> list[BlockMatch]:
    results: list[BlockMatch] = []
    for rule in rules:
        if rule.mode == "regex":
            try:
                pattern = re.compile(rule.value, re.IGNORECASE)
            except re.error:
                continue
        else:
            parts = [part for part in re.split(r"\s+", rule.value) if part]
            pattern = re.compile(r"\s+".join(re.escape(part) for part in parts), re.IGNORECASE)
        direct = list(pattern.finditer(text))
        if direct:
            for match in direct:
                results.append(
                    BlockMatch(rule.value, match.group(0), match.start(), match.end())
                )
            continue

        # OCR and office conversion can change full-width characters. This
        # fallback preserves recall; the evidence block remains the source of truth.
        normalized_text = unicodedata.normalize("NFKC", text).casefold()
        if rule.mode == "literal":
            normalized_keyword = unicodedata.normalize("NFKC", rule.value).casefold()
            start = normalized_text.find(normalized_keyword)
            if start >= 0:
                results.append(
                    BlockMatch(rule.value, rule.value, max(0, start), max(0, start) + len(rule.value))
                )
    return results
