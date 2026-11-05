"""静默模式：验过码之后，把"要验证码的关卡"关一段时间。

## 这是什么

    正常情况：动作守卫 / 提权 / 危险操作 / 审计员 / 外传
              碰到就发验证码到邮箱。

    但用户可能在赶一个活，一小时里被弹五次码谁都受不了。

    所以：**码验过之后，用户可以说"关到五点"** ——
         不是只放行这一次，而是把上面那五道全关到那个点。

## ⚠️ 三条铁律

    一、**关也要码。**

        不是"用户说一句就关"。用户必须先拿到邮箱里的码、
        填对，然后"关到几点"才生效。

        为什么：如果能不验码就关，那攻击者只要在对话里说
        "帮我关一下验证码"就绕过了整套体系。
        关闸门，本身就是最危险的操作。

    二、"用户说了就一定要关"。

        码验过之后，用户说关多久就关多久，**不做二次确认**。
        用户已经用邮箱证明过自己是本人了。

    三、到点自动恢复，不用谁记得。

        静默状态是**存盘**的（带到期时间戳），
        进程重启也不会丢，到点了自然失效。

## 时长怎么来

    **用户自己说**，不提供固定选项。

        "关到下午五点"      → 算到 17:00
        "关两小时"          → 算到 now + 7200
        "关到明天早上"      → 算到次日 08:00

    解析交给前台（Agent 会理解中文），
    这里只收一个**绝对时间戳**，不猜语义。

## 跟安全模式的关系

    「出事可回退」模式 = 五道永远关着，只靠 VSS 兜底。
    静默模式         = 临时关一段时间，到点回来。

    两者是**叠加**的：安全模式是常驻选择，静默是一次性的临时授权。
"""

from __future__ import annotations

import json
import os
import time

# 静默最长给多久（防止用户说"关到 2099 年"然后忘了）。
#
# ⚠️ 为什么要有上限：
#     用户说"关到明天下午"是正常需求，
#     但如果说"关到明年"，那等于永久关了 ——
#     这种多半是手滑或者没想清楚。
#     24 小时够覆盖任何一段正经的办公时间。
MAX_SECONDS = 24 * 3600


def _path() -> str:
    from tools import userdirs

    return userdirs.config_file("quiet_until.json")


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


def _save(d: dict) -> None:
    p = _path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except OSError:
        pass


def until() -> float:
    """静默到什么时候。0 = 没在静默。"""
    d = _load()
    try:
        ts = float(d.get("until") or 0)
    except (TypeError, ValueError):
        return 0.0
    # 过期的自动当没有
    if ts <= time.time():
        return 0.0
    return ts


def remaining() -> int:
    """还剩多少秒。0 = 没在静默。"""
    u = until()
    return max(0, int(u - time.time())) if u else 0


def is_quiet() -> bool:
    """现在静默中吗 —— 所有发码的地方都先问这个。"""
    return until() > 0


def status() -> dict:
    """给界面/前台看。"""
    u = until()
    d = _load()
    return {
        "quiet": u > 0,
        "until": u,
        "remaining": remaining(),
        "why": d.get("why") or "" if u > 0 else "",
    }


def start(seconds: int, why: str = "") -> tuple[bool, str]:
    """开始静默。

    ⚠️ **调用方必须先验过码。**

        这个函数本身不做任何校验 —— 它就是个"把时间写进去"的动作。
        真正的前台流程是：
            用户填码 → verify() 通过 → 才调这里。

        所以它在 guard_tools 里只被"码验过之后"的分支调用，
        不直接暴露成"用户说一句就关"的工具。

    返回 (成功, 说明)。
    """
    try:
        secs = int(seconds)
    except (TypeError, ValueError):
        return False, "时间不对"

    if secs <= 0:
        return False, "得给一个正数时长"

    if secs > MAX_SECONDS:
        secs = MAX_SECONDS

    u = time.time() + secs
    _save({
        "until": u,
        "why": why or "用户验码后关闭验证",
        "at": time.time(),
    })

    h, m = divmod(secs // 60, 60)
    dur = (f"{h} 小时 {m} 分钟" if h else f"{m} 分钟")
    until_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(u))

    return True, f"已经关掉验证，{dur}后（{until_str}）自动恢复。"


def stop(why: str = "") -> tuple[bool, str]:
    """立刻结束静默，恢复正常验证。

    这个**不用码** —— 恢复保护永远是安全的，不需要证明什么。
    """
    _save({"until": 0, "why": why or "手动恢复", "at": time.time()})
    return True, "验证已经恢复。"
