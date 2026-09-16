"""驗證美元估價、快取折扣、逐次長上下文門檻與不完整資料。"""
import pytest

from api_pricing import estimate_response_usd, format_cost
from test_token_usage import NOW, context, ledger, meta, report, response, scan, write


@pytest.mark.parametrize("model,expected", [
    ("gpt-6-astra", 1.49), ("gpt-5.6-sol", .596),
    ("gpt-5.6", .596), ("gpt-5.6-terra", .33), ("gpt-5.6-luna", .033),
])
def test_cached_read_write_and_output_are_billed_once(model, expected):
    # 一般輸入 40k、快取讀取 40k、寫入 20k、輸出 16k。
    assert estimate_response_usd(model, 100_000, 40_000, 20_000, 16_000) == pytest.approx(expected)


def test_long_context_threshold_uses_entire_response():
    assert estimate_response_usd("gpt-6-astra", 272_000, 200_000, 20_000, 10_000) == pytest.approx(1.47)
    assert estimate_response_usd("gpt-6-astra", 272_001, 200_000, 20_000, 10_000) == pytest.approx(2.69002)
    assert estimate_response_usd("gpt-5.5", 100_000, 80_000, None, 10_000) == pytest.approx(.44)


@pytest.mark.parametrize("model,counts", [
    ("unknown-model", (100, 80, 0, 20)),
    ("gpt-6-astra-preview", (100, 80, 0, 20)),
    ("gpt-6-astra", (100, None, 0, 20)),
    ("gpt-6-astra", (100, 80, None, 20)),
    ("gpt-6-astra", (100, 80, 30, 20)),
    ("gpt-6-astra", (100, -1, 0, 20)),
    ("gpt-6-astra", (100, True, 0, 20)),
    ("gpt-5.5", (100, 80, 10, 20)),
])
def test_unknown_prices_and_invalid_details_are_not_zero(model, counts):
    assert estimate_response_usd(model, *counts) is None


def test_cost_format_distinguishes_empty_missing_partial_and_small_amounts():
    assert format_cost(0, 0, 0) == "—"
    assert format_cost(0, 0, 2) == "無法估算"
    assert format_cost(0, 1, 1) == "約 US$0.00"
    assert format_cost(.001, 1, 1) == "< US$0.01"
    assert format_cost(1234.567, 1, 2) == "約 US$1,234.57（部分）"


def test_ledger_prices_before_aggregation_and_keeps_totals_consistent(ledger):
    store, collector, path = ledger
    records = [meta(), context(model="gpt-6-astra")]
    for i in range(2):
        records.append(response(str(i), input_tokens=200_000, cached_input_tokens=100_000,
            cache_write_input_tokens=0, output_tokens=10_000, total_tokens=210_000))
    records += [context("long", "gpt-6-astra"), response("long", "long", input_tokens=300_000,
        cached_input_tokens=200_000, cache_write_input_tokens=0, output_tokens=10_000, total_tokens=310_000),
        response("missing", input_tokens=100, cached_input_tokens=None),
        context("unknown", "unknown-model"), response("unknown", "unknown")]
    write(path, records)
    scan(collector)
    result = report(store)
    # 兩次短回應各 $1.60，一次長回應 $3.15；缺漏與未知不當成零。
    assert result.api_cost_usd == pytest.approx(6.35)
    assert result.priced_responses == 3 and result.responses == 5
    group = next(g for g in result.groups if g.model == "gpt-6-astra")
    assert group.api_cost_usd == pytest.approx(6.35) and group.priced_responses == 3
    rows, count = store.turns("all", "gpt-6-astra", "low", now=NOW)
    assert count == 2
    assert sum(row["api_cost_usd"] for row in rows) == pytest.approx(group.api_cost_usd)
    assert sum(row["priced_responses"] for row in rows) == group.priced_responses
    # 重複掃描不重複計價，舊資料庫不必重新匯入就能取得估價。
    scan(collector)
    assert report(store).api_cost_usd == pytest.approx(6.35)
    from token_usage import TokenUsageStore
    assert TokenUsageStore(store.path).report("all", NOW).api_cost_usd == pytest.approx(6.35)


def test_price_follows_selected_period_and_ambiguous_models_are_excluded(ledger):
    store, collector, path = ledger
    write(path, [meta(), context(model="gpt-6-astra"), response(),
        context("older", "gpt-6-astra"), response("older", "older", stamp="2026-09-01T01:00:00Z")])
    scan(collector)
    assert store.report("today", NOW).priced_responses == 1
    assert store.report("all", NOW).priced_responses == 2
    write(path, [context(model="different-model")], "a")
    scan(collector)
    assert store.report("today", NOW).priced_responses == 0
