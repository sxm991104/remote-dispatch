"""把"联网授权"接到验证码上。

## 缺的那一环

    netpolicy.grant()        有了 —— 放行 30 分钟
    _net_fallback_allowed()  有了 —— 没授权就拒绝
    **但没人能让用户"填码授权"。**

    这一环不接上，沙箱一旦不可用，用户就只能看着命令被拒，
    没有任何办法放行 —— 那等于把功能锁死了。

## 怎么做

    复用现有的验证码机制（guard.send_code_email + guard.verify）：

        一、命令被拒 → 返回一句"需要联网授权"
        二、用户在前台回验证码
        三、前台转给服务端 /api/net/allow
        四、服务端验码 → netpolicy.grant()

    ⚠️ 码在服务端验，不在前台验。
       跟审核员那套一样：前台只负责转达。

## 为什么用独立的码，不跟任务端/审核员共用

    三个地方各自管自己的码（都是"错三次锁死"那套逻辑）：

        任务端码   放行那一个危险动作
        审核端码   关掉审核一段时间
        联网码     放行联网一段时间

    共用的话，填了任务端的码，联网也跟着开了 —— 那是错的。
    计数也会互相连累。
"""

from __future__ import annotations

import json
import os
import secrets
import time

# 码有效期
CODE_TTL = 600

# 错几次锁死
MAX_WRONG = 3


def _path() -> str:
    from tools import userdirs

    return userdirs.config_file("net_code.json")


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


def ensure_code() -> tuple[str, bool]:
    """拿一个待用的码。返回 (码, 是不是新的)。

    ⚠️ 10 分钟内已经发过就复用 —— 用户可能手慢，或者邮件延迟。
        每要一次就换一个码，他手上的就失效了，越急越乱。
    """
    d = _load()
    code = str(d.get("code") or "")
    expires = float(d.get("expires") or 0)

    if code and time.time() < expires:
        return code, False

    code = f"{secrets.randbelow(1_000_000):06d}"
    _save({
        "code": code,
        "expires": time.time() + CODE_TTL,
        "wrong": 0,
        "created": time.time(),
    })
    return code, True


def peek() -> dict:
    d = _load()
    return {
        "has": bool(d.get("code")) and time.time() < float(d.get("expires") or 0),
        "wrong": int(d.get("wrong") or 0),
    }


def verify(code: str) -> tuple[bool, str]:
    """验码。对了就清掉（一次一用）。"""
    code = (code or "").strip()
    d = _load()
    real = str(d.get("code") or "")
    expires = float(d.get("expires") or 0)

    if not real:
        return False, "没有等待确认的联网请求"

    if time.time() >= expires:
        _save({})
        return False, "验证码过期了"

    if not code:
        return False, "没填验证码"

    if secrets.compare_digest(code, real):
        _save({})
        return True, "验证通过"

    wrong = int(d.get("wrong") or 0) + 1
    left = MAX_WRONG - wrong

    if left <= 0:
        _save({})
        # 跟另外两处一个规矩：错三次锁死
        try:
            from guard import auditor_state as _st

            _st.mark_locked(f"联网验证码连续填错 {MAX_WRONG} 次")
        except Exception:  # noqa: BLE001
            pass
        return False, f"填错 {MAX_WRONG} 次了，任务端已锁死"

    d["wrong"] = wrong
    _save(d)
    return False, f"码不对，还能再试 {left} 次"


def clear() -> None:
    _save({})


def send_code() -> tuple[bool, str]:
    """发码到用户邮箱。返回 (发出去了没, 说明)。"""
    code, is_new = ensure_code()
    if not is_new:
        return True, "码已经发过了（10 分钟内有效）"

    try:
        from guard import emailer

        body = (
            "有人想放行「联网执行」一段时间。\n\n"
            "沙箱是断网的 —— 这是刻意的。有些活确实要网\n"
            "（pip install、git clone、下载数据），所以要你点头。\n\n"
            f"验证码：{code}\n\n"
            "如果你确实要放行：把这个码回复给前台，再选一个时长。\n"
            "如果你没要求过 —— 什么都别做，让命令被拒就行。\n"
        )
        return emailer.send("联网授权验证码", body)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
