"""Validation of classifier output against a category set."""

import json
import logging
import re

from shared.categories import FALLBACK_CATEGORY

log = logging.getLogger("labeler")


def parse_categories(raw: str | None, n: int, valid_names: list[str],
                     on_failure: str | None = FALLBACK_CATEGORY) -> list[str | None]:
    """Parse the LLM's JSON array; invalid entries fall back to the Other category.

    A response with no array at all (empty, or error text instead of an answer)
    yields `on_failure` for every email: Other by default, or None to leave those
    emails unlabeled so a later run can retry them."""
    if not raw:
        log.warning("Empty LLM response for %d emails", n)
        return [on_failure] * n
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    try:
        parsed = json.loads(match.group(0) if match else raw)
        assert isinstance(parsed, list)
    except (json.JSONDecodeError, AssertionError):
        log.warning("Unparseable LLM response %r", raw[:200])
        return [on_failure] * n
    out = []
    for i in range(n):
        cat = parsed[i] if i < len(parsed) else None
        if cat not in valid_names:
            log.warning("Invalid category %r for email %d; using %s",
                        cat, i + 1, FALLBACK_CATEGORY)
            cat = FALLBACK_CATEGORY
        out.append(cat)
    return out
