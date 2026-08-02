"""Email classification for the web product — Anthropic API (paid, per-token).

Backfills go through the Message Batches API (50% price, async — a job is not
latency-sensitive). Small daily sweeps use synchronous calls. Model is Haiku by
default for unit economics; set MODEL in .env to change.
"""

import logging
import time

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from web.config import ANTHROPIC_API_KEY, MODEL
from shared.categories import build_system_prompt, build_user_prompt, category_names
from shared.parsing import parse_categories

GROUP_SIZE = 10  # emails per model call
POLL_SECONDS = 30

log = logging.getLogger("web")


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def group_emails(emails: list[dict]) -> list[list[dict]]:
    return [emails[i:i + GROUP_SIZE] for i in range(0, len(emails), GROUP_SIZE)]


def _params(system_prompt: str, group: list[dict]) -> MessageCreateParamsNonStreaming:
    return MessageCreateParamsNonStreaming(
        model=MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": build_user_prompt(group)}],
    )


def _result_text(message) -> str | None:
    return next((b.text for b in message.content if b.type == "text"), None)


def classify_batch_api(categories: list[tuple[str, str]], groups: list[list[dict]],
                       on_progress=None, usage_out: list | None = None) -> list[list[str]]:
    """Classify all groups via the Batches API. Returns one category list per group.

    If usage_out is a list, (input_tokens, output_tokens) per succeeded request is
    appended to it — used by the benchmark to report real cost."""
    client = _client()
    system_prompt = build_system_prompt(categories)
    names = category_names(categories)

    batch = client.messages.batches.create(requests=[
        Request(custom_id=f"g{i}", params=_params(system_prompt, group))
        for i, group in enumerate(groups)
    ])
    log.info("Submitted batch %s (%d groups)", batch.id, len(groups))

    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        if on_progress:
            on_progress(batch.request_counts.succeeded + batch.request_counts.errored)
        time.sleep(POLL_SECONDS)

    by_id: dict[str, str | None] = {}
    for result in client.messages.batches.results(batch.id):
        if result.result.type == "succeeded":
            by_id[result.custom_id] = _result_text(result.result.message)
            if usage_out is not None:
                usage = result.result.message.usage
                usage_out.append((usage.input_tokens, usage.output_tokens))
        else:
            log.warning("Batch item %s: %s", result.custom_id, result.result.type)
            by_id[result.custom_id] = None

    return [parse_categories(by_id.get(f"g{i}"), len(group), names)
            for i, group in enumerate(groups)]


def classify_sync(categories: list[tuple[str, str]], groups: list[list[dict]],
                  usage_out: list | None = None) -> list[list[str]]:
    """Classify groups with synchronous calls — for small daily sweeps."""
    client = _client()
    system_prompt = build_system_prompt(categories)
    names = category_names(categories)
    out = []
    for group in groups:
        try:
            message = client.messages.create(**_params(system_prompt, group))
            raw = _result_text(message)
            if usage_out is not None:
                usage_out.append((message.usage.input_tokens, message.usage.output_tokens))
        except anthropic.APIError as e:
            log.warning("Sync classify failed (%s); falling back", e)
            raw = None
        out.append(parse_categories(raw, len(group), names))
    return out
