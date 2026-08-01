"""Unit tests for the benchmark report (pure summarize function — no network)."""

from web.benchmark import price_for, summarize


def rows_fixture():
    mk = lambda truth, pred, i: {"truth": truth, "pred": pred,
                                 "sender": f"s{i}@example.com", "subject": f"subject {i}"}
    rows = [mk("Marketing", "Marketing", i) for i in range(8)]
    rows += [mk("Newsletter", "Marketing", i) for i in range(2)]  # the classic confusion
    return rows


def test_summarize_quality_and_cost_math():
    usage = [(2000, 50), (1000, 25)]  # 3000 in / 75 out tokens
    report = summarize(rows_fixture(), usage, "claude-haiku-4-5",
                       batch_mode=True, days=365)
    assert "Overall agreement: 8/10 = 80.0%" in report
    assert "Newsletter -> Marketing: 2" in report
    assert "Input tokens:  3,000" in report
    # haiku batch: (3000*1 + 75*5)/1e6 * 0.5 = $0.0016875 -> rounded 0.0017
    assert "$0.0017" in report
    assert "Extrapolated" in report and "10,000 emails" in report


def test_summarize_sync_mode_full_price():
    report = summarize(rows_fixture(), [(3000, 75)], "claude-haiku-4-5",
                       batch_mode=False, days=90)
    assert "synchronous (full price)" in report
    assert "$0.0034" in report  # 2x the batch figure


def test_summarize_unknown_model_prints_tokens_only():
    report = summarize(rows_fixture(), [(100, 10)], "claude-mystery-9",
                       batch_mode=True, days=365)
    assert "Unknown pricing" in report


def test_price_for_prefix_match():
    assert price_for("claude-haiku-4-5") == (1.00, 5.00)
    assert price_for("claude-haiku-4-5-20251001") == (1.00, 5.00)
    assert price_for("claude-sonnet-4-5") == (3.00, 15.00)
    assert price_for("gpt-42") is None
