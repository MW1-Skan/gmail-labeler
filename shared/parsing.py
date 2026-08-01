"""Validation of classifier output against a category set."""

import json
import logging
import re

from shared.categories import FALLBACK_CATEGORY

log = logging.getLogger("labeler")


def parse_categories(raw: str | None, n: int, valid_names: list[str]) -> list[str]:
    """Parse the LLM's JSON array; invalid entries fall back to the Other category."""
    if not raw:
        log.warning("Empty LLM response; falling back to %s for %d emails",
                    FALLBACK_CATEGORY, n)
        return [FALLBACK_CATEGORY] * n
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    try:
        parsed = json.loads(match.group(0) if match else raw)
        assert isinstance(parsed, list)
    except (json.JSONDecodeError, AssertionError):
        log.warning("Unparseable LLM response %r; falling back to %s",
                    raw[:200], FALLBACK_CATEGORY)
        return [FALLBACK_CATEGORY] * n
    out = []
    for i in range(n):
        cat = parsed[i] if i < len(parsed) else None
        if cat not in valid_names:
            log.warning("Invalid category %r for email %d; using %s",
                        cat, i + 1, FALLBACK_CATEGORY)
            cat = FALLBACK_CATEGORY
        out.append(cat)
    return out
