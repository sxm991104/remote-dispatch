"""真的调模型审一次。

## 这一步做什么

    把改动整理成文本 → 送给模型 → 解析返回的 JSON。

## ⚠️ 送进去的只有改动本身

    不送：任务描述、项目说明、上一次审核的结论、用户说过的话。
    送了任何一条，它就能被"解释"，而这正是我们要防的。

## ⚠️ 模型返回必须能解析

    它是个"程序"，不是聊天对象。要的是一行 JSON。
    模型有时会套 ```json 代码块、有时会多说两句 ——
    得剥掉这些壳再解析。

    解析不出来怎么办：按配置里的 on_error。
        "allow"  放行（不能因为它返回乱了就把用户卡死）
        "deny"   拦住（安全优先）
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

# 单次审核的超时（秒）。
#
# ⚠️ 不能太长：这是卡在用户派活的路径上，超过了不如放行。
TIMEOUT = 45

# 改动文本的上限（字符）。
#
# 大文件（几万行）全塞进去会爆 token，而且没必要 ——
# 我们看的是"改了什么"，不是"整个文件长什么样"。
MAX_DIFF_CHARS = 24000


def build_changes_text(changes: dict) -> tuple[str, str]:
    """把改动整理成一个"改动描述"，外加一段客观信息。

    返回 (正文, meta)。

    ⚠️ 这里**不能**读整个文件、不能调 AI —— 只做文本整理。
        真正的判断在模型那边。
    """
    import os

    parts: list[str] = []
    files: list[str] = []

    # 删除
    for p in changes.get("deletes") or []:
        parts.append(f"=== 删除：{p}")
        files.append(p)

    # 新文件
    for _shadow, real in changes.get("writes") or []:
        files.append(real)
        try:
            n = os.path.getsize(_shadow)
        except OSError:
            n = 0
        head = _read_head(_shadow, 2000)
        parts.append(
            f"=== 新建：{real}（{n} 字节）\n"
            f"--- 内容开头 ---\n{head}"
        )

    # 修改
    for shadow, real in changes.get("modifies") or []:
        files.append(real)
        old_txt = _read_head(real, 4000)
        new_txt = _read_head(shadow, 4000)

        try:
            old_n = os.path.getsize(real)
        except OSError:
            old_n = 0
        try:
            new_n = os.path.getsize(shadow)
        except OSError:
            new_n = 0

        # 少了多少 —— 这是"覆盖/抹除"最直接的证据。
        shrink = ""
        if old_n > 0 and new_n * 4 < old_n:
            pct = int((1 - new_n / old_n) * 100)
            shrink = f"\n⚠️ 原来 {old_n} 字节，现在 {new_n} 字节（少了 {pct}%）"

        parts.append(
            f"=== 修改：{real}（{old_n} → {new_n} 字节）{shrink}\n"
            f"--- 改之前 ---\n{old_txt}\n"
            f"--- 改之后 ---\n{new_txt}"
        )

    text = "\n\n".join(parts)
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + "\n…（后面截断了）"

    meta = f"这次一共动了 {len(files)} 个文件。"
    return text, meta


def _read_head(path: str, limit: int) -> str:
    """读文件开头一小段。读不了就返回空。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return "（读不了）"


def _parse_json(raw: str) -> dict | None:
    """从模型的返回里挖出那个 JSON。

    ⚠️ 模型经常不老实：
        套 ```json 代码块
        前面说一句"好的，我来分析"
        多个 JSON 粘在一起

        这几种都剥掉。剥不干净就返回 None。
    """
    if not raw:
        return None

    s = raw.strip()

    # 去掉代码块围栏
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
        s = s.strip()

    # 直接试
    try:
        d = json.loads(s)
        if isinstance(d, dict):
            return d
    except json.JSONDecodeError:
        pass

    # 从里面挖第一个 {...}
    m = re.search(r"\{.*\}", s, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict):
                return d
        except json.JSONDecodeError:
            pass

    return None


def _call_model(cfg: dict, system: str, user: str) -> str:
    """真的发一次请求。返回模型的原始文本。"""
    base = str(cfg.get("base_url") or "").rstrip("/")

    # ⚠️ 不要对某一家特判（我第一版写了 `if base.endswith("deepseek.com")`，
    #    结果 moonshot 拼出来是 /chat/completions，少个 /v1，必然 404）。
    #
    #    OpenAI 兼容接口的规矩是：
    #        地址里已经带版本号了（/v1、/compatible-mode/v1 之类）
    #            → 只接 /chat/completions
    #        只是个域名（api.deepseek.com、api.moonshot.cn）
    #            → 补上 /v1/chat/completions
    #
    #    判断"带没带版本号"用正则，不猜具体域名。
    if re.search(r"/v\d+$", base) or "/compatible-mode" in base:
        url = base + "/chat/completions"
    else:
        url = base + "/v1/chat/completions"

    body = {
        "model": cfg.get("model"),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
    }

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.get('api_key')}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        got = json.loads(r.read())

    # OpenAI 兼容格式
    choices = got.get("choices") or []
    if not choices:
        raise ValueError(f"模型没返回 choices：{str(got)[:200]}")
    return (choices[0].get("message") or {}).get("content") or ""
