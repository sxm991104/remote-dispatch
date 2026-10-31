"""查一个接口上有哪些模型可用。

为什么要做这个
    用户填模型名全靠猜：Kimi 以前叫 moonshot-v1-8k，现在叫 kimi-k2.6。
    填错了报 404，报错还是英文的 "Not found the model"，
    用户根本不知道该填什么。

    改成：Key 填好就自动去问接口"你有哪些模型"，拉回来做成下拉框。
    这是最不会出错的做法——列表就是服务商自己给的。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .llm import _normalize_base


def list_models(base_url: str, api_key: str) -> tuple[bool, list[str], str]:
    """列出这个接口可用的模型。

    返回 (成功, 模型名列表, 错误信息)。

    走的是 OpenAI 的 /v1/models 接口，几乎所有兼容服务都实现了。
    有些服务不实现或者需要额外权限，那种情况返回失败，
    界面退回手动输入。
    """
    if not base_url or not api_key:
        return False, [], "地址和 Key 都要填"

    # 从 chat/completions 地址反推 /models 地址
    chat_url = _normalize_base(base_url)
    models_url = chat_url.replace("/chat/completions", "/models")

    req = urllib.request.Request(
        models_url,
        headers={"Authorization": f"Bearer {api_key}"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return False, [], f"查不到模型列表（HTTP {e.code}）{detail}"
    except Exception as e:  # noqa: BLE001
        return False, [], f"查不到模型列表：{type(e).__name__}"

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return False, [], "接口返回的格式不是预期的样子"

    # ⚠️ 有的服务返回两条：id（调接口用）+ name（给人看）。
    #
    #    DeepSeek 官方就是这样：
    #        id   = "deepseek-flash"
    #        name = "DeepSeek-V4.1-Flash"
    #
    #    原来只取 id，下拉框里全是 deepseek-flash——
    #    跟官方界面上显示的名字对不上，用户会以为查错了。
    #
    #    现在优先取 name 显示，id 留着调接口。
    #    没有 name 的服务（大多数）就用 id 当显示名。
    pairs: list[tuple[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        mid = str(it.get("id") or "").strip()
        label = str(it.get("name") or "").strip()
        if not mid and not label:
            continue
        if not mid:
            mid = label
        if not label:
            label = mid
        pairs.append((mid, label))

    # 按显示名排序，重复的按 id 去重
    seen = set()
    uniq: list[tuple[str, str]] = []
    for mid, label in sorted(pairs, key=lambda x: x[1].lower()):
        if mid in seen:
            continue
        seen.add(mid)
        uniq.append((mid, label))

    if not uniq:
        return False, [], "接口没返回任何模型"

    # 返回的是 [(id, label)]，调用方自己决定怎么用
    return True, uniq, ""


def pick_default(models: list[str]) -> str:
    """从模型列表里挑一个合适的默认值。

    优先级：
        1. 名字里带 chat 的（对话模型，不是 embedding / tts）
        2. 排除明显的非对话模型（embedding、whisper、tts、rerank、vision-only）
        3. 剩下的按名字排第一个
    """
    if not models:
        return ""

    skip_words = ("embed", "whisper", "tts", "rerank", "moderation", "audio")

    def _id_of(m):
        return m[0] if isinstance(m, (tuple, list)) else str(m)

    def _label_of(m):
        return m[1] if isinstance(m, (tuple, list)) and len(m) > 1 else _id_of(m)

    def usable(m) -> bool:
        low = (_id_of(m) + " " + _label_of(m)).lower()
        return not any(w in low for w in skip_words)

    candidates = [m for m in models if usable(m)]
    if not candidates:
        candidates = list(models)

    # 优先带 chat / flash 的（对话模型）
    for m in candidates:
        low = (_id_of(m) + " " + _label_of(m)).lower()
        if "chat" in low or "flash" in low:
            return _id_of(m)

    return _id_of(candidates[0])
