"""Read official Standard text prices as data; keep the last validated snapshot."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from types import MappingProxyType
import urllib.request

from api_pricing import MODEL_PRICES, PRICING_DATE, ModelPrice

SOURCE_URL = "https://developers.openai.com/api/docs/pricing.md"
MAX_BYTES = 2 * 1024 * 1024
DAY = 86400
RETRY = 3600


def _money(value):
    if value in ("-", "—"):
        return None
    if not re.fullmatch(r"\$(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?", value):
        raise ValueError("Invalid USD rate")
    rate = Decimal(value[1:].replace(",", "")) * 1000
    if not 0 <= rate <= 1_000_000_000:
        raise ValueError("USD rate outside supported range")
    return int(rate) if rate == int(rate) else rate


def parse_official_prices(markdown: str) -> dict[str, ModelPrice]:
    """Only explicitly named Standard tables; never infer prices from prose."""
    threshold = re.search(r"Short context:\s*≤\s*(\d+)K input tokens", markdown)
    threshold = int(threshold[1]) * 1000 if threshold else None
    prices = {}
    standard = False
    headers = None
    rich = False
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            # All following tier/subsection headings end the current table.
            standard = line == "### Standard pricing data"
            headers = None
            continue
        if not standard or not line.startswith("|"):
            continue
        cells = [part.strip() for part in line.strip("|").split("|")]
        if cells[0] == "Model":
            if len(set(cells)) != len(cells):
                raise ValueError("Duplicate price columns")
            headers = cells
            rich = "Short context input" in headers
            required = ({"Model", "Short context input", "Short context output",
                         "Short context cached input", "Short context 30m write",
                         "Long context input", "Long context output",
                         "Long context cached input", "Long context 30m write"}
                        if rich else {"Model", "Input", "Output", "Cached input"})
            if not required.issubset(headers) or (rich and not threshold):
                raise ValueError("Unsupported Standard pricing columns or context threshold")
            continue
        if headers is None or all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        if len(cells) != len(headers):
            raise ValueError("Incomplete price row")
        row = dict(zip(headers, cells))
        model = re.sub(r"\s+\(<\d+K context length\)$", "", row["Model"])
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*", model):
            raise ValueError("Unsupported model identifier")
        prefix = "Short context " if rich else ""
        inp, out = _money(row[prefix + "input" if rich else "Input"]), _money(row[prefix + "output" if rich else "Output"])
        cached = _money(row[prefix + "cached input" if rich else "Cached input"])
        write = _money(row["Short context 30m write"]) if rich else None
        if inp is None or out is None:
            continue
        long_rates = (tuple(_money(row["Long context " + key]) for key in
                            ("input", "cached input", "30m write", "output")) if rich else None)
        price = ModelPrice(inp, cached, write, out, threshold if rich else None, long_rates)
        if model in prices and prices[model] != price:
            raise ValueError("Conflicting Standard prices")
        prices[model] = price
    if not prices:
        raise ValueError("No supported Standard text prices")
    return prices


class _OfficialRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if newurl != SOURCE_URL:
            raise ValueError("Unexpected pricing redirect")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def fetch_official_prices() -> str:
    request = urllib.request.Request(SOURCE_URL, headers={
        "User-Agent": "Quota-PromptDock/pricing", "Accept": "text/markdown"})
    with urllib.request.build_opener(_OfficialRedirects()).open(request, timeout=10) as response:
        if response.geturl() != SOURCE_URL:
            raise ValueError("Unexpected pricing source")
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("Pricing document too large")
    return body.decode("utf-8")


@dataclass(frozen=True)
class PriceView:
    prices: object
    checked_at: float = 0
    failed: bool = False

    @property
    def note(self):
        if self.checked_at:
            stamp = datetime.fromtimestamp(self.checked_at, timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
            first = f"官方價格 · {stamp}" + ("（同步失敗，沿用快取）" if self.failed else " 已同步")
            return first + f"\n未列出的既有模型沿用 {PRICING_DATE} 內建價格；歷史用量以目前價格比較。"
        return f"內建價格 · {PRICING_DATE}" + ("（同步失敗，稍後重試）" if self.failed else "（等待官方同步）")


class PriceCatalog:
    def __init__(self, path: Path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self._lock = threading.Lock()
        self._view = PriceView(MappingProxyType(dict(MODEL_PRICES)))
        try:
            if self.path.stat().st_size > MAX_BYTES:
                raise ValueError("Oversized price cache")
            record = json.loads(self.path.read_text(encoding="utf-8"))
            checked = record["checked_at"]
            if (record["version"] != 1 or record["source"] != SOURCE_URL or
                    type(checked) not in (int, float) or not math.isfinite(checked) or
                    not 0 < checked <= self.clock() + 300):
                raise ValueError("Invalid price cache")
            self._view = self._validated_view(record["markdown"], checked)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError):
            self.mark_failed()

    def _validated_view(self, text, checked):
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_BYTES:
            raise ValueError("Invalid cached document")
        prices = dict(MODEL_PRICES)
        prices.update(parse_official_prices(text))
        return PriceView(MappingProxyType(prices), checked)

    def snapshot(self):
        with self._lock:
            return self._view

    def mark_failed(self):
        with self._lock:
            view = self._view
            self._view = PriceView(view.prices, view.checked_at, True)

    def update(self, text):
        checked = self.clock()
        view = self._validated_view(text, checked)
        record = {"version": 1, "source": SOURCE_URL, "checked_at": checked, "markdown": text}
        payload = json.dumps(record, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_BYTES:
            raise ValueError("Price cache too large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staged = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, prefix=".prices-", delete=False) as handle:
                staged = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(staged, self.path)
        finally:
            if staged is not None:
                staged.unlink(missing_ok=True)
        with self._lock:
            self._view = view


class PriceSynchronizer:
    def __init__(self, catalog, on_change, *, fetch=fetch_official_prices, clock=time.time):
        self.catalog, self.on_change, self.fetch, self.clock = catalog, on_change, fetch, clock
        self._stop, self._wake, self._lock = threading.Event(), threading.Event(), threading.Lock()
        self._next = catalog.snapshot().checked_at + DAY if catalog.snapshot().checked_at else 0
        self._last_attempt = 0.0
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="OfficialModelPrices", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=0.1)

    def request(self, *, force=False):
        with self._lock:
            if self.clock() - self._last_attempt >= (60 if force else RETRY):
                self._next = 0
        self._wake.set()

    def attempt_if_due(self):
        with self._lock:
            if self.clock() < self._next or self._stop.is_set():
                return False
            self._last_attempt = self.clock()
        try:
            text = self.fetch()
            if self._stop.is_set():
                return False
            self.catalog.update(text)
            delay = DAY
        except Exception:
            if self._stop.is_set():
                return False
            self.catalog.mark_failed()
            delay = RETRY
        with self._lock:
            self._next = self.clock() + delay
        self.on_change()
        return True

    def _run(self):
        while not self._stop.is_set():
            self._wake.clear()
            self.attempt_if_due()
            with self._lock:
                delay = max(0.1, min(60, self._next - self.clock()))
            self._wake.wait(delay)
