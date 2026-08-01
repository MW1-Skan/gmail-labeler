"""Category definitions and classifier prompt building.

A category set is a list of (name, description) pairs. The personal labeler uses
DEFAULT_CATEGORIES; the web product builds a per-user set from its database.
"""

FALLBACK_CATEGORY = "Other"

DEFAULT_CATEGORIES: list[tuple[str, str]] = [
    ("Newsletter",
     "editorial newsletters and content digests the user deliberately subscribed to and "
     "reads — curated articles, analysis, dev/AI/tech digests (Substack, TLDR, Pragmatic "
     "Engineer, The Batch, etc.). Editorial digest content goes here, never to Dev. "
     "Promotional brand mail is NOT Newsletter — that is Marketing."),
    ("Marketing",
     "promotional and commercial bulk mail — sales, discounts, coupons, product "
     "announcements, welcome emails, re-engagement campaigns (\"we miss you\"), "
     "loyalty-program promos, brand blasts. If an email is ambiguous between Newsletter "
     "and Marketing (e.g. a brand mixing content with promotion, job-alert digests), "
     "choose Marketing."),
    ("Shopping",
     "transactional purchase mail ONLY — order confirmations, receipts, invoices for "
     "purchases, shipping/delivery updates, refunds and returns. Promotional mail from "
     "shops or e-commerce brands is Marketing, NOT Shopping."),
    ("Papers",
     "government, official paperwork, taxes administration, fines, CAF, secu, utilities."),
    ("Finance",
     "any mail from banks or financial institutions."),
    ("Dev",
     "transactional developer-tool mail only (GitHub, CI/CD, monitoring, cloud "
     "providers, package registries). NOT newsletters."),
    ("Security",
     "verification/2FA codes, one-time passwords, login alerts, password resets."),
    ("Immo",
     "real estate — apartment listings, agency contacts, visit confirmations, "
     "SeLoger/PAP/Leboncoin immo alerts."),
    (FALLBACK_CATEGORY,
     "everything else, including personal mail from friends, mail from myself, and "
     "anything ambiguous."),
]

_PROMPT_HEADER = """You are an email classifier. You will receive a numbered list of emails \
(sender, subject, and a short excerpt of the body). Classify EACH email into exactly one \
of these categories:
"""

_PROMPT_FOOTER = """
Each email belongs to one and only one category.

Respond with ONLY a JSON array of category names, one per email, in the same order as the \
input. No explanations, no markdown fences, nothing else. Example: ["{example_a}", "{example_b}"]"""


def category_names(categories: list[tuple[str, str]]) -> list[str]:
    return [name for name, _ in categories]


def build_system_prompt(categories: list[tuple[str, str]]) -> str:
    """Render the classifier system prompt for an arbitrary category set."""
    names = category_names(categories)
    if FALLBACK_CATEGORY not in names:
        raise ValueError(f"category set must include the {FALLBACK_CATEGORY!r} fallback")
    lines = "\n".join(f"- {name}: {desc}" for name, desc in categories)
    footer = _PROMPT_FOOTER.format(example_a=names[0], example_b=FALLBACK_CATEGORY)
    return _PROMPT_HEADER + "\n" + lines + "\n" + footer


def build_user_prompt(emails: list[dict]) -> str:
    """Render the numbered email list sent alongside the system prompt."""
    return "\n".join(
        f"{i}. From: {e['sender']}\n   Subject: {e['subject']}\n   Excerpt: {e['excerpt']}"
        for i, e in enumerate(emails, 1)
    )
