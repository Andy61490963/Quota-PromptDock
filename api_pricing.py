"""以公開 Standard API 單價估算本機 Token 的美元等值，不代表實際帳單。"""
from __future__ import annotations

from dataclasses import dataclass

PRICING_DATE = "2026-09-16"
PRICING_URL = "https://developers.openai.com/api/docs/pricing"
LONG_CONTEXT_THRESHOLD = 272_000


@dataclass(frozen=True)
class ModelPrice:
    # 每 Token 的十億分之一美元，使用整數計算，顯示時才四捨五入。
    input_rate: int
    cached_rate: int
    write_rate: int | None
    output_rate: int


# 來源：官方價格頁，以及 https://developers.openai.com/api/docs/models/gpt-5.5。
# 僅接受已核對的模型識別碼，未確認的模型不可套用其他模型的價格。
MODEL_PRICES = {
    "gpt-6-astra": ModelPrice(10_000, 1_000, 12_500, 50_000),
    "gpt-5.6-sol": ModelPrice(4_000, 400, 5_000, 20_000),
    "gpt-5.6": ModelPrice(4_000, 400, 5_000, 20_000),
    "gpt-5.6-terra": ModelPrice(2_000, 200, 2_500, 12_000),
    "gpt-5.6-luna": ModelPrice(200, 20, 250, 1_200),
    "gpt-5.5": ModelPrice(5_000, 500, None, 30_000),
    "gpt-5.5-2026-04-23": ModelPrice(5_000, 500, None, 30_000),
}


def estimate_response_usd(model, input_tokens, cached_tokens, write_tokens, output_tokens) -> float | None:
    """單次回應先計價再加總；未知單價或必要明細缺漏時不猜測。"""
    price = MODEL_PRICES.get(model)
    if price is None:
        return None
    if price.write_rate is None:
        # 未公布快取寫入單價的舊模型，只接受未記錄寫入或明確為零。
        if write_tokens not in (None, 0):
            return None
        write_tokens = 0
    values = (input_tokens, cached_tokens, write_tokens, output_tokens)
    if any(type(value) is not int or value < 0 for value in values):
        return None
    if cached_tokens + write_tokens > input_tokens:
        return None
    uncached = input_tokens - cached_tokens - write_tokens
    input_cost = (uncached * price.input_rate + cached_tokens * price.cached_rate
                  + write_tokens * (price.write_rate or 0))
    output_cost = output_tokens * price.output_rate
    if input_tokens > LONG_CONTEXT_THRESHOLD:
        input_cost *= 2
        output_cost = output_cost * 3 // 2
    return (input_cost + output_cost) / 1_000_000_000


def format_cost(usd: float, priced: int, responses: int) -> str:
    if not responses:
        return "—"
    if not priced:
        return "無法估算"
    amount = "< US$0.01" if 0 < usd < 0.01 else f"約 US${usd:,.2f}"
    return amount + ("（部分）" if priced < responses else "")


def pricing_description(usd: float, priced: int, responses: int) -> str:
    return (
        f"API 等值估算：{format_cost(usd, priced, responses)}\n"
        f"已估算 {priced:,} / {responses:,} 次回應；未涵蓋部分不視為零。\n"
        f"採 {PRICING_DATE} 核對的 Standard 文字 Token 美元單價，歷史用量也以此價格比較。\n"
        "逐次計算：（輸入 − 快取讀取 − 快取寫入）× 輸入單價 + 快取讀取 × 快取單價"
        " + 快取寫入 × 寫入單價 + 輸出 × 輸出單價。推理已含於輸出，不重複收費。\n"
        "單次輸入超過 272,000 Token 時，輸入與快取費率 ×2、輸出費率 ×1.5。\n"
        "未知模型、缺少必要快取明細或明細不一致的回應不計價。\n"
        "此為 API 對照估算，非訂閱帳單；不含 Fast／Priority 加價、Batch／Flex 折扣、"
        "地區加價、工具費、稅金及未收錄的用量。\n"
        f"官方價格：{PRICING_URL}"
    )
