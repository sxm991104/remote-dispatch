"""审核端专用的验证码。

## 为什么不能跟任务端的码共用

    两件事看着像，管的东西不一样：

        任务端的码   放行**那一个危险动作**
                    填了 → 那个动作执行

        审核端的码   关掉**审核一段时间**
                    填了 → 接下来 N 小时不审

    如果共用一个码，填了任务端的码，审核也跟着关了 —— 那是错的。
    而且计数会互相连累：一边填错三次，另一边也被锁。

## 所以：复用机制，不共用品

    复用    发信通道（同一个 SMTP 配置、同一套邮件模板）
            生成 6 位码
            「错三次锁死」这个逻辑

    分开    码本身（各存各的）
            计数（各数各的）
            管什么（一个管动作，一个管审核窗口）

## 存在哪

    audit_code.json，跟 guard.json 同一个目录。

    里面最多一条（审核端同时只有一个待确认的码 ——
    因为状态是全局的，见 auditor_state.py）。
"""

from __future__ import annotations

import json
import os
import secrets
import time

# 码的有效期。跟任务端那边一致（10 分钟）。
CODE_TTL = 600

# 错几次锁死。跟任务端那边一致。
MAX_WRONG = 3


def _path() -> str:
    from tools import userdirs

    return userdirs.config_file("audit_code.json")


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


def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def ensure_code() -> tuple[str, bool]:
    """拿一个待确认的码。没有就生成。

    返回 (码, 是不是新生成的)。

    ⚠️ 为什么不是每次都生成新的

        用户可能手慢，或者邮件延迟。
        每点一次就换一个码，他手上的那个就失效了 —— 越急越乱。

        所以：10 分钟内已经发过一个有效的，就复用它。
    """
    d = _load()
    code = str(d.get("code") or "")
    expires = float(d.get("expires") or 0)

    if code and time.time() < expires:
        return code, False

    code = _new_code()
    _save({
        "code": code,
        "expires": time.time() + CODE_TTL,
        "wrong": int(d.get("wrong") or 0),
        "created": time.time(),
    })
    return code, True


def peek() -> dict:
    """看一眼现在有没有活的码。"""
    d = _load()
    return {
        "has": bool(d.get("code")) and time.time() < float(d.get("expires") or 0),
        "wrong": int(d.get("wrong") or 0),
        "expires": float(d.get("expires") or 0),
    }


def verify(code: str) -> tuple[bool, str]:
    """验码。返回 (对不对, 说明)。

    ⚠️ 错三次直接锁死任务端。

        跟任务端那边是同一个规矩，只是**各数各的**。
        任务端填错三次不会连累审核端，反过来也一样。
    """
    code = (code or "").strip()
    d = _load()
    real = str(d.get("code") or "")
    expires = float(d.get("expires") or 0)

    if not real:
        return False, "没有等待确认的验证码，先让审核员命中一次"

    if time.time() >= expires:
        _save({})   # 清掉过期的
        return False, "验证码过期了，等一下会收到新的"

    if not code:
        return False, "没填验证码"

    if secrets.compare_digest(code, real):
        # 对了：清掉这个码（一次一用）
        _save({})
        return True, "验证通过"

    # 错了：计数
    wrong = int(d.get("wrong") or 0) + 1
    left = MAX_WRONG - wrong

    if left <= 0:
        _save({})
        from guard import auditor_state as st

        st.mark_locked(f"审核验证码连续填错 {MAX_WRONG} 次")
        return False, f"填错 {MAX_WRONG} 次了，任务端已锁死"

    d["wrong"] = wrong
    _save(d)
    return False, f"码不对，还能再试 {left} 次"


def clear() -> None:
    _save({})
