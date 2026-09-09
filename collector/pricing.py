"""成本估算。價格表讀 collector/pricing.json，不得寫死在程式碼裡（SPEC §6）。"""
import json
from pathlib import Path
from typing import Dict, Any, List, Tuple


DEFAULT_PRICING_PATH = Path(__file__).parent / "pricing.json"


def load_table(path=None):
    """讀 pricing.json。"""
    if path is None:
        path = DEFAULT_PRICING_PATH
    else:
        path = Path(path)

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        # 找不到或格式錯誤時回傳空表，讓呼叫端處理
        return {"version": "unknown", "models": {}}


def estimate_cost_by_model(by_model: Dict[str, Dict[str, int]],
                           table: Dict[str, Any]) -> Tuple[Dict[str, float], List[str]]:
    """回傳 (每個模型的美金金額 dict, 錯誤訊息清單)。

    這是成本算法的唯一實作：分項與總額都從這裡來，不得另寫一份乘法。

    用量全為 0 的模型直接跳過（沒有金額可算，不是錯誤）；
    真的有用量卻找不到價格的模型不計入金額，並留一條中文訊息——
    絕不可靜默當 0。
    """
    models = table.get("models", {})
    per_model: Dict[str, float] = {}
    errors = []

    for model_name, usage in by_model.items():
        if (usage.get("input_tokens", 0) == 0
                and usage.get("output_tokens", 0) == 0
                and usage.get("cache_creation_input_tokens", 0) == 0
                and usage.get("cache_read_input_tokens", 0) == 0):
            # 用量全為 0，沒有金額可算，直接跳過不報錯
            continue
        if model_name not in models:
            errors.append(f"模型 {model_name} 無價格資料，不計入成本估算")
            continue

        prices = models[model_name]
        # 價格單位：每百萬 token 美元
        input_price = prices.get("input", 0) / 1_000_000
        output_price = prices.get("output", 0) / 1_000_000
        cache_write_price = prices.get("cache_write", 0) / 1_000_000
        cache_read_price = prices.get("cache_read", 0) / 1_000_000

        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)

        per_model[model_name] = (input_tokens * input_price +
                                 output_tokens * output_price +
                                 cache_creation * cache_write_price +
                                 cache_read * cache_read_price)

    return per_model, errors


def estimate_cost(by_model: Dict[str, Dict[str, int]], table: Dict[str, Any]) -> Tuple[float, List[str]]:
    """回傳 (美金總金額, 錯誤訊息清單)。

    總額即分項相加，算法只有 estimate_cost_by_model 這一套。
    """
    per_model, errors = estimate_cost_by_model(by_model, table)
    return sum(per_model.values()), errors