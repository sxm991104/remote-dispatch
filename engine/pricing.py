"""真实花销：按标准 OpenAI 的计价方式算钱。

## 为什么不是"估算"

    原来的做法：读会话文件体积，除以 1.6，乘 DeepSeek 的价。
    问题三个：
        · 只认一种价，但用户可能配了 Kimi、通义、硅基流动
        · 永远按"未命中缓存"算，有缓存命中时会贵十倍地估
        · 会话文件体积 ≠ 实际用量

    现在的做法：**接口每次都回 usage，那才是真的用了多少。**

## 标准 OpenAI 的计价模型

    一次调用的花销
        = 输入（未命中缓存）  × price_in  / 1e6
        + 输入（命中缓存）    × price_cached / 1e6
        + 输出                × price_out / 1e6

    其中
        未命中 = prompt_tokens - cached_tokens
        命中   = cached_tokens

    DeepSeek / Kimi / 通义 都照这个模型，只是单价不同。

## 价格从哪来

    每套模型配置里存三个数（每百万 token 的美元价）：
        price_in        输入
        price_cached    输入·命中缓存（不填 = price_in × 0.1）
        price_out       输出

    没填就用 _DEFAULT_PRICES 里按模型名匹配的默认值 ——
    常见模型都有一份，用户不用手填。

## ⚠️ 一句话说清楚：这是"算出来的"账单，不是"厂商开的"账单

    厂商的实际账单还会算：
        · 图片 / 音频（我们没有）
        · 批量折扣（没用）
        · 汇率浮动

    但按 usage × 官方单价算出来的数，精度在"分"这一级，
    对"今天花了多少"这个问题足够。
"""

from __future__ import annotations

import json
import os
import time

# ── 默认价目（每百万 token，美元）──
#
# 按模型名的**小写子串**匹配，从长到短找。
# 价取自各家官方定价页（2026 年的公开价），
# 用户在自己的模型配置里填了就以填的为准。
_DEFAULT_PRICES = {
    # ── OpenAI ──
    "gpt-4o-mini": (0.15, 0.015, 0.60),
    "gpt-4o": (2.50, 1.25, 10.00),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "o3-mini": (1.10, 0.55, 4.40),
    "o1-mini": (1.10, 0.55, 4.40),

    # ── DeepSeek ──
    "deepseek-chat": (0.27, 0.027, 1.10),
    "deepseek-flash": (0.27, 0.027, 1.10),
    "deepseek-reasoner": (0.55, 0.055, 2.19),

    # ── Kimi（Moonshot）──
    "kimi-k2": (0.60, 0.15, 2.50),
    "moonshot-v1-8k": (0.20, 0.02, 0.20),
    "moonshot-v1-32k": (0.40, 0.04, 0.40),

    # ── 通义 ──
    "qwen-max": (0.35, 0.035, 1.40),
    "qwen-plus": (0.11, 0.011, 0.28),
    "qwen-turbo": (0.05, 0.005, 0.11),

    # ── 硅基流动（按他家统一价，多数开源模型）──
    "deepseek-ai/deepseek-v3": (0.27, 0.027, 1.10),
}

# 完全匹配不上时的兜底价（按 DeepSeek 的价，不会离谱到哪去）
_FALLBACK = (0.27, 0.027, 1.10)

# 美元 → 人民币。做成可配置，不写死在代码里。
_RATE_FILE = "usd_cny_rate.json"
_DEFAULT_RATE = 7.1


def _cfg_dir() -> str:
    from tools import userdirs

    return os.path.dirname(userdirs.config_file("x.json"))


def _ledger_path() -> str:
    from tools import userdirs

    return userdirs.config_file("usage_ledger.jsonl")


def rate() -> float:
    """美元兑人民币汇率。用户可以在配置里改。"""
    p = os.path.join(_cfg_dir(), _RATE_FILE)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        v = float(d.get("rate") or 0)
        if v > 0:
            return v
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return _DEFAULT_RATE


def set_rate(v: float) -> tuple[bool, str]:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return False, "汇率得是个数字"
    if not (1 < v < 30):
        return False, "汇率看着不对（应该在 1 到 30 之间）"
    p = os.path.join(_cfg_dir(), _RATE_FILE)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"rate": v}, f)
        os.replace(tmp, p)
    except OSError as e:
        return False, f"存不住：{e}"
    return True, f"汇率改成 {v}"


def prices_for(profile: dict) -> tuple[float, float, float]:
    """这套模型的三档价（每百万 token，美元）。

    优先用配置里填的；没填就按模型名找默认价。
    """
    p_in = profile.get("price_in")
    p_c = profile.get("price_cached")
    p_out = profile.get("price_out")

    try:
        p_in = float(p_in) if p_in not in (None, "") else None
    except (TypeError, ValueError):
        p_in = None
    try:
        p_c = float(p_c) if p_c not in (None, "") else None
    except (TypeError, ValueError):
        p_c = None
    try:
        p_out = float(p_out) if p_out not in (None, "") else None
    except (TypeError, ValueError):
        p_out = None

    if p_in is None or p_out is None:
        model = str(profile.get("model") or "").lower()
        d_in, d_c, d_out = _FALLBACK
        # 从长到短找，避免 "gpt-4o" 抢了 "gpt-4o-mini" 的价
        for key in sorted(_DEFAULT_PRICES, key=len, reverse=True):
            if key in model:
                d_in, d_c, d_out = _DEFAULT_PRICES[key]
                break
        if p_in is None:
            p_in = d_in
        if p_out is None:
            p_out = d_out
        if p_c is None:
            p_c = d_c

    # 缓存价没填就按输入价的 1/10（行业通行）
    if p_c is None:
        p_c = p_in * 0.1

    return float(p_in), float(p_c), float(p_out)


def cost_usd(profile: dict, prompt_tokens: int, completion_tokens: int,
             cached_tokens: int = 0) -> float:
    """这一次调用花了多少美元。"""
    p_in, p_c, p_out = prices_for(profile)

    pt = max(0, int(prompt_tokens or 0))
    ct = max(0, int(completion_tokens or 0))
    ca = max(0, min(int(cached_tokens or 0), pt))

    fresh = pt - ca
    usd = (
        fresh * p_in / 1_000_000
        + ca * p_c / 1_000_000
        + ct * p_out / 1_000_000
    )
    return round(usd, 8)


def record(profile_id: str, model: str, task_name: str,
           prompt_tokens: int, completion_tokens: int,
           cached_tokens: int = 0, prices: dict | None = None) -> float:
    """记一笔用量。返回这次花的美元（不是必须用，方便调试）。

    prices 是这套配置的三档价（可能全是 None，那就按模型名匹配默认价）。

    ⚠️ 为什么会有一份"账本"文件

        任务跑完就结束了，state 里只留最后状态。
        "今天花了多少"要跨任务累加 —— 得有地方按天存。

        用 jsonl 追加，一行一次调用，好处：
            · 并发写不会互相覆盖（只追加）
            · 出问题能一行行查
            · 用户自己能打开看
    """
    prof = {"model": model}
    if prices:
        for k in ("price_in", "price_cached", "price_out"):
            if prices.get(k) not in (None, ""):
                prof[k] = prices[k]

    usd = cost_usd(prof, prompt_tokens, completion_tokens, cached_tokens)
    row = {
        "ts": time.time(),
        "day": time.strftime("%Y-%m-%d"),
        "profile": profile_id or "",
        "model": model or "",
        "task": task_name or "",
        "pt": int(prompt_tokens or 0),
        "ct": int(completion_tokens or 0),
        "ca": int(cached_tokens or 0),
        "usd": usd,
    }
    try:
        p = _ledger_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8", newline="") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    return usd


def today() -> dict:
    """今天的总花销（所有模型加起来）。

    返回：
        usd      今天总共花了多少美元
        cny      折成人民币
        calls    调了多少次
        tokens   总共用了多少 token
        by_model {模型名: 美元}
    """
    day = time.strftime("%Y-%m-%d")
    usd = 0.0
    calls = 0
    tokens = 0
    by_model: dict = {}

    p = _ledger_path()
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("day") != day:
                    continue
                u = float(r.get("usd") or 0)
                usd += u
                calls += 1
                tokens += int(r.get("pt") or 0) + int(r.get("ct") or 0)
                m = str(r.get("model") or "未知")
                by_model[m] = round(by_model.get(m, 0.0) + u, 8)
    except OSError:
        pass

    rt = rate()
    return {
        "day": day,
        "usd": round(usd, 6),
        "cny": round(usd * rt, 4),
        "rate": rt,
        "calls": calls,
        "tokens": tokens,
        "by_model": by_model,
    }
