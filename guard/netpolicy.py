"""网络策略：沙箱里断网，要联网得用户点头。

## 为什么需要这个

    沙箱里现在**硬断网**（NetworkAccess=*,Block + WFP 内核级）。

    沙箱断网是好事 —— 词表挡不住 python -c "import requests; ..."，
    断网能。

    但有些活**确实要网**：
        pip install xxx
        git clone xxx
        下载数据做分析

    所以不能一刀切。

## 三套方案，选了 A

    A  一律断网 + 用户填码才放行联网        ← 选这个
    B  两个沙箱（断网 / 联网）
    C  白名单放行 git.exe / pip.exe

    为什么不选 B：
        用哪个沙箱是代码判断，代码被改就绕了。
        而如果让用户选，那跟 A 一样都要用户参与 —— 那就直接用 A，少一层。

    为什么不选 C：
        **白名单不是"开个小口"，是开个大门。**
            git 能 clone → 也能把数据 push 到攻击者的仓库
            pip 能装包   → 也能装恶意包
        这些都是真实的攻击手法。

    A 的关键：
        攻击者要绕，必须先拿到验证码。
        而验证码在用户邮箱里。
        → 用户不参与，就绕不过。

## 实现

    联网不是"改配置"，是"**这一次换个执行方式**"：

        沙箱里跑    断网         默认
        普通执行    有网         用户填码才允许

    ⚠️ 普通执行**不等于**没有防护。
       动作守卫（词表 + 路径 + 权限）照常生效。
       少的只是沙箱那一层（改动不再隔离）。
"""

from __future__ import annotations

import json
import os
import time

# 一次放行多久。到点自动收回，不是永久的。
GRANT_TTL = 30 * 60      # 30 分钟

# 状态文件
def _path() -> str:
    from tools import userdirs

    return userdirs.config_file("net_grant.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save(d: dict) -> None:
    p = _path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError:
        pass


def granted() -> tuple[bool, int]:
    """现在有没有联网授权。返回 (有没, 还剩多少秒)。"""
    d = _load()
    until = float(d.get("until") or 0)
    left = int(until - time.time())
    if left <= 0:
        return False, 0
    return True, left


def grant(minutes: int = GRANT_TTL // 60, why: str = "") -> float:
    """放行联网一段时间。返回到期时间戳。

    ⚠️ 调用方必须**先验过验证码**才能调这个。
       这个模块自己不管验码 —— 那是 guard 的事。
       （跟 auditor 那套一样：码在服务端验，不在这儿。）
    """
    until = time.time() + minutes * 60
    _save({"until": until, "why": why, "since": time.time()})
    return until


def revoke(why: str = "") -> None:
    """收回授权。"""
    _save({"until": 0, "why": why or "收回", "since": time.time()})


def status() -> dict:
    """给界面和日志看。"""
    ok, left = granted()
    d = _load()
    return {
        "granted": ok,
        "remaining": left,
        "why": d.get("why") or "",
        "ttl": GRANT_TTL,
    }
