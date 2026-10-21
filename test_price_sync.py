"""Official prices, offline cache, future models, and bounded background work."""
import json
import threading

import pytest

from api_pricing import estimate_response_usd
import price_sync as ps
from test_token_usage import context, ledger, meta, report, response, scan, write


def flat(model="future-sol", inp="$2.00", out="$10.00", cached="$0.10"):
    return ("### Standard pricing data\n\n| Model | Input | Output | Cached input |\n"
            "| --- | --- | --- | --- |\n"
            f"| {model} | {inp} | {out} | {cached} |\n")


def rich(model="future-sol", threshold=272):
    return ("### Standard pricing data\n\n"
            "| Model | Short context input | Short context output | Short context cached input | Short context 30m write | Short context 12h write | Long context input | Long context output | Long context cached input | Long context 30m write | Long context 12h write |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
            f"| {model} | $3 | $9 | $0.2 | $4 | $8 | $11 | $24 | $1 | $15 | $30 |\n"
            f"Short context: ≤{threshold}K input tokens. Long context: >{threshold}K input tokens.\n")


def cost(prices, model="future-sol", inp=1_000_000, cached=0, write=0, out=0):
    return estimate_response_usd(model, inp, cached, write, out, prices=prices)


def test_only_standard_text_tables_and_literal_long_rates_are_used():
    text = (rich(threshold=400) + "### Batch pricing data\n" + flat(inp="$0.01").split("\n", 1)[1]
            + "### Fast pricing data\n" + flat(inp="$999").split("\n", 1)[1])
    prices = ps.parse_official_prices(text)
    assert cost(prices, inp=400_000) == pytest.approx(1.2)
    assert cost(prices, inp=400_001) == pytest.approx(4.400011)
    assert cost(prices, inp=400_001, cached=400_001) == pytest.approx(.400001)
    assert cost(prices, inp=400_001, write=400_001) == pytest.approx(6.000015)
    assert cost(prices, inp=400_001, out=100_000) == pytest.approx(6.800011)


def test_flat_model_has_no_invented_long_context_surcharge_or_write_rate():
    prices = ps.parse_official_prices(flat(inp="$3"))
    assert cost(prices) == pytest.approx(3)
    assert cost(prices, write=1) is None
    prices = ps.parse_official_prices(flat(cached="-"))
    assert cost(prices, cached=1) is None
    assert cost(prices) == pytest.approx(2)


def test_sub_nanodollar_prices_are_not_rounded_to_free():
    prices = ps.parse_official_prices(flat(inp="$0.0005"))
    assert cost(prices) == pytest.approx(.0005)


def test_missing_long_prices_remain_unpriced():
    text = rich().replace("$11 | $24 | $1 | $15 | $30", "- | - | - | - | -")
    prices = ps.parse_official_prices(text)
    assert cost(prices) is None
    assert cost(prices, inp=100_000) == pytest.approx(.3)


@pytest.mark.parametrize("bad", [
    "<html>Sign in</html>", "", flat().replace("$2.00", "NaN"),
    flat().replace("$2.00", "€2"), flat().replace("$2.00", "$-2"),
    flat().replace("$2.00", "$1,,2"), flat().replace("$2.00", "$1000001"),
    flat().replace("Output", "Unknown column"), flat().replace("future-sol", "model with spaces"),
    flat().replace("| $0.10 |", "|"), rich().replace("Short context: ≤272K input tokens.", ""),
    flat() + flat(inp="$3"),
])
def test_invalid_document_keeps_last_good_cache_and_rates(tmp_path, bad):
    catalog = ps.PriceCatalog(tmp_path / "prices.json")
    catalog.update(flat())
    before = catalog.path.read_bytes()
    with pytest.raises(ValueError):
        catalog.update(bad)
    assert catalog.path.read_bytes() == before
    assert cost(catalog.snapshot().prices) == pytest.approx(2)


def test_atomic_write_failure_keeps_previous_snapshot(tmp_path, monkeypatch):
    catalog = ps.PriceCatalog(tmp_path / "prices.json")
    catalog.update(flat())
    before = catalog.path.read_bytes()

    def denied(*args):
        raise OSError("read only")

    monkeypatch.setattr(ps.os, "replace", denied)
    with pytest.raises(OSError):
        catalog.update(flat(inp="$3"))
    assert catalog.path.read_bytes() == before
    assert cost(catalog.snapshot().prices) == pytest.approx(2)
    assert not list(tmp_path.glob(".prices-*"))


def test_cache_reloads_offline_and_failure_does_not_reset_its_date(tmp_path):
    now = [1_000_000]
    catalog = ps.PriceCatalog(tmp_path / "prices.json", clock=lambda: now[0])
    catalog.update(flat())
    reloaded = ps.PriceCatalog(catalog.path, clock=lambda: now[0])
    assert cost(reloaded.snapshot().prices) == pytest.approx(2)
    calls, changed = [], []

    def offline():
        calls.append(True)
        raise OSError("offline")

    sync = ps.PriceSynchronizer(reloaded, lambda: changed.append(True), fetch=offline, clock=lambda: now[0])
    assert not sync.attempt_if_due()
    now[0] += ps.DAY
    assert sync.attempt_if_due()
    assert "同步失敗" in reloaded.snapshot().note and reloaded.snapshot().checked_at == 1_000_000
    assert cost(reloaded.snapshot().prices) == pytest.approx(2)
    now[0] += ps.RETRY - 1
    assert not sync.attempt_if_due()
    now[0] += 1
    assert sync.attempt_if_due() and len(calls) == len(changed) == 2


def test_daily_refresh_and_manual_throttle(tmp_path):
    now = [1_000_000]
    catalog = ps.PriceCatalog(tmp_path / "prices.json", clock=lambda: now[0])
    calls = []
    sync = ps.PriceSynchronizer(catalog, lambda: None,
                               fetch=lambda: calls.append(True) or flat(), clock=lambda: now[0])
    assert sync.attempt_if_due()
    sync.request(force=True)
    assert not sync.attempt_if_due()
    now[0] += 60
    sync.request(force=True)
    assert sync.attempt_if_due()
    now[0] += ps.DAY - 1
    assert not sync.attempt_if_due()
    now[0] += 1
    assert sync.attempt_if_due() and len(calls) == 3


def test_corrupt_or_foreign_cache_uses_builtin_then_repairs(tmp_path):
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"version": 1, "source": "https://example.test", "checked_at": 1, "markdown": flat()}))
    catalog = ps.PriceCatalog(path)
    assert "future-sol" not in catalog.snapshot().prices
    assert "gpt-6.1-sol" in catalog.snapshot().prices
    sync = ps.PriceSynchronizer(catalog, lambda: None, fetch=lambda: flat())
    assert sync.attempt_if_due() and "future-sol" in catalog.snapshot().prices
    assert not catalog.snapshot().failed


def test_existing_ledger_reprices_new_model_without_reimport(ledger, tmp_path):
    store, collector, path = ledger
    catalog = ps.PriceCatalog(tmp_path / "prices.json")
    store.pricing = catalog
    write(path, [meta(), context(model="future-sol"), response()])
    scan(collector)
    assert report(store).priced_responses == 0
    catalog.update(flat())
    after = report(store)
    assert after.responses == after.priced_responses == 1
    assert after.api_cost_usd == pytest.approx(.000248)
    assert "官方價格" in after.pricing_note
    rows, count = store.turns("all", "future-sol", "low")
    assert count == 1 and rows[0]["api_cost_usd"] == pytest.approx(after.api_cost_usd)
    with store.connection() as db:
        old = db.execute("SELECT api_cost('future-sol',1000000,0,0,0)").fetchone()[0]
        catalog.update(flat(inp="$3"))
        assert db.execute("SELECT api_cost('future-sol',1000000,0,0,0)").fetchone()[0] == old
    assert cost(catalog.snapshot().prices) == pytest.approx(3)


def test_shutdown_during_download_does_not_write_cache_or_callback(tmp_path):
    entered, release = threading.Event(), threading.Event()
    catalog = ps.PriceCatalog(tmp_path / "prices.json")
    changed = []

    def slow_fetch():
        entered.set()
        release.wait(5)
        return flat()

    sync = ps.PriceSynchronizer(catalog, lambda: changed.append(True), fetch=slow_fetch)
    sync.start()
    assert entered.wait(2)
    sync.stop()
    release.set()
    sync._thread.join(timeout=2)
    assert not sync._thread.is_alive() and not changed and not catalog.path.exists()


@pytest.mark.parametrize("url,body", [("https://example.test/pricing.md", b"bad"),
                                     (ps.SOURCE_URL, b"x" * (ps.MAX_BYTES + 1))], ids=["wrong-origin", "oversized"])
def test_download_rejects_wrong_origin_and_oversized_documents(monkeypatch, url, body):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return url
        def read(self, length): return body[:length]

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == ps.SOURCE_URL and timeout == 10
            return Response()

    monkeypatch.setattr(ps.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError):
        ps.fetch_official_prices()


def test_official_redirects_do_not_leave_the_fixed_pricing_endpoint():
    with pytest.raises(ValueError):
        ps._OfficialRedirects().redirect_request(None, None, 302, "", {}, "https://example.test")
