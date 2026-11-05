"""安全模式能不能开 —— 按**真实能力**判断，不按愿望判断。

## 三种模式各自依赖什么

    严格防护            要能发验证码
        拦下来之后要问"你确定吗"，码发不出去就只能干等，
        用户什么都干不了 —— 等于把机器锁死。
        → **依赖：验证码邮箱**

    出事可回退          要能回退
        它不拦、不要码，全靠"出事能退回去"兜底。
        没有回退能力时选它 = **裸奔**。
        → **依赖：系统卷影副本**

    全开                要能回退 + 要能发码
        命令直接在真机跑，什么都不拦。
        → **依赖：系统卷影副本 + 验证码邮箱**

## ⚠️ 为什么这件事必须硬判

    老爸的原话（2026-11-05）：

        「邮箱没填比如关闭，应该要跳到出事可回退；
         如果影子执行、vss插件没有，有邮箱，那就只能走严格防护；
         如果都没有设置，就给提示，都开不了，只有配置了才能开。」

    如果界面允许用户选一个实际做不到的模式，那他得到的是
    **虚假的安全感** —— 以为有保护，其实没有。
    这比明说"没保护"更危险。

## ⚠️ 判断失败时倾向于"不能开"

    查不到 = 不能开。
    宁可让用户看到"要先去配"，也不要让他以为配好了。
"""

from __future__ import annotations

import time

# ── 结果缓存 ──
#
# ⚠️ 为什么必须缓存
#
#     _rewind_ready() 要扫 Copy1..Copy64 找卷影副本，
#     每次都是 64 条 PowerShell 查询 —— 实测十几秒。
#
#     而 capabilities() 会被调很多次：
#          · 服务端每次切模式都查
#          · 桌面端刷新状态也查
#          · 前台工具也查
#
#     不缓存的话，用户点一下「严格防护」要转十几秒才响应。
#     （2026-11-05 老爸说「安全模式桌面端切换老慢了」。）
#
# 缓存多久：30 秒。
#     用户配完邮箱/开完系统保护，三十秒内就能看到变化 ——
#     比"每次都要等十几秒"好得多。
_CACHE: dict = {}
_CACHE_TTL = 30.0


def _cached(key: str, fn):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) < _CACHE_TTL:
        return hit[1]

    val = fn()
    _CACHE[key] = (now, val)
    return val


def clear_cache() -> None:
    """清掉缓存（用户刚改完配置时调一下）。"""
    _CACHE.clear()


def _email_ready() -> tuple[bool, str]:
    """验证码邮箱配好了没。

    ## ⚠️ 光有邮箱地址不够

        三样都要：
            · enabled 开关打开（用户可以在桌面端关掉它）
            · 邮箱地址
            · SMTP 授权码

        缺任何一样，码都发不出去 ——
        而"严格防护"这个模式**每拦一次就要发一个码**。
        发不出去 = 用户卡在那儿什么都干不了。

        （2026-11-05 老爸说「我邮箱关了，还能切换到严格防护」——
          这是错的：关了邮箱还能切过去，用户就再也出不来了。
          因为守卫会一直拦，要码又发不出来。）
    """

    def _check() -> tuple[bool, str]:
        try:
            from guard import guard as g

            cfg = g.load_config() or {}

            # 开关没开 → 码发不出去
            if not cfg.get("enabled"):
                return False, (
                    "验证码开关是关着的（桌面端「安全」页那个「开启」）—— "
                    "关了之后守卫拦下来也没法发码，人就被卡死了"
                )

            email = str(cfg.get("email") or "").strip()
            if not email:
                return False, "没配验证码邮箱"

            pwd = str(cfg.get("smtp_pass") or "").strip()
            if not pwd:
                return False, f"邮箱 {email} 填了，但 SMTP 授权码没填，码发不出去"

            host = str(cfg.get("smtp_host") or "").strip()
            if not host:
                return False, "发信服务器没填（一般填了邮箱会自动认出来，这个没认出来）"

            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, f"查不到邮箱配置：{type(e).__name__}"

    return _cached("email", _check)


def _rewind_ready() -> tuple[bool, str]:
    """版本回退能不能用 —— 看系统卷影副本。

    ## ⚠️ 为什么查的是"卷影副本"而不是我们自己存的东西

        我们**自己不存任何历史版本**。
        Windows 的系统保护会自动给盘建卷影副本，
        guard/history.py 只是去读它。

        所以判断"能不能回退"= 判断"这台机器上有没有卷影副本"。

        实测（2026-11-05）：
            普通权限就能查（history.find_versions 走 PowerShell），
            不需要管理员。

    ## ⚠️ 这一步很慢，必须缓存

        find_versions 会一次性扫 Copy1..Copy64（一次 PowerShell 调用
        里跑 64 条语句），实测 3-8 秒。

        调用方被 _cached 包住，30 秒内复用结果。
    """

    def _probe() -> tuple[bool, str]:
        try:
            from guard import history

            import os

            probe_file = ""
            for cand in (
                r"C:\Windows\System32\notepad.exe",
                r"C:\Windows\System32\cmd.exe",
                r"C:\Windows\explorer.exe",
            ):
                if os.path.exists(cand):
                    probe_file = cand
                    break

            if not probe_file:
                return False, "找不到可用来探测卷影副本的文件"

            # ⚠️ 直接调 find_versions，**不先调 available()**。
            #
            #    实测（2026-11-05）：
            #        available()      0.34 秒（起一次 PowerShell）
            #        find_versions()  0.53 秒（再起一次）
            #        两个都调         0.87 秒
            #
            #    available() 做的事是「PowerShell 能不能跑」——
            #    而 find_versions 本来就要起 PowerShell，
            #    它跑得通就说明 PowerShell 没问题。
            #    所以那 0.34 秒纯属重复。
            #
            #    现在只调一次：能查到版本 = 可用；
            #    查不到 = 要么没 PowerShell、要么没卷影副本 ——
            #    这时候再看具体原因（那一步只在慢路径上跑一次）。
            vs = history.find_versions(probe_file)
            if vs:
                return True, ""

            # 一个版本都没有：可能是没 PowerShell，也可能是没卷影副本。
            # 这时候才去问 available() 要个准确原因（慢一点没关系，
            # 反正是"开不了"这条路，用户本来就要去改配置）。
            ok, why = history.available()
            if not ok:
                return False, why

            return False, (
                "这台机器上还没有任何卷影副本 —— 得先开「系统保护」"
                "（控制面板 → 系统 → 系统保护 → 配置 → 启用）"
            )
        except Exception as e:  # noqa: BLE001
            return False, f"查不到卷影副本：{type(e).__name__}: {e}"

    return _cached("rewind", _probe)


def capabilities() -> dict:
    """三项能力的真实状态。

    返回：
        {
            "email":  {"ok": bool, "why": str},
            "rewind": {"ok": bool, "why": str},
            "modes":  {"strict": {...}, "rollback": {...}, "both": {...}},
        }

    每个 mode 是 {"ok": bool, "why": str}：
        ok=True   能选
        ok=False  不能选，why 说明缺什么
    """
    e_ok, e_why = _email_ready()
    r_ok, r_why = _rewind_ready()

    modes = {
        "strict": {
            "ok": e_ok,
            "why": "" if e_ok else f"严格防护要发验证码，但{e_why}",
        },
        "rollback": {
            "ok": r_ok,
            "why": "" if r_ok else f"出事可回退靠系统卷影副本兜底，但{r_why}",
        },
        "both": {
            "ok": e_ok and r_ok,
            "why": "" if (e_ok and r_ok) else (
                "全开不拦不要码，全靠能回退；现在"
                + ("没配邮箱" if not e_ok else "")
                + ("且" if (not e_ok and not r_ok) else "")
                + ("没有卷影副本" if not r_ok else "")
            ),
        },
    }

    return {
        "email": {"ok": e_ok, "why": e_why},
        "rewind": {"ok": r_ok, "why": r_why},
        "modes": modes,
    }


def any_available(caps: dict | None = None) -> bool:
    """有没有任何一个模式能用。"""
    c = caps or capabilities()
    return any(m["ok"] for m in c["modes"].values())


# 模式的强弱顺序。数字越大保护越多。
#
# ⚠️ 前台只能"往保护多的方向"切，不能往少的方向切。
#    （2026-11-05 老爸定的：前台只能开，不能关。）
STRENGTH = {
    "strict": 3,      # 保护最多
    "both": 2,
    "rollback": 1,    # 保护最少（只有回退兜底）
}


def downgrade_of(cur: str, want: str) -> bool:
    """从 cur 换成 want 是不是在降低保护。"""
    return STRENGTH.get(want, 0) < STRENGTH.get(cur, 0)


# 降级时该往哪个模式走（按保护强度从高到低）
FALLBACK_ORDER = ("strict", "both", "rollback")


def pick_usable(cur: str, caps: dict | None = None) -> str:
    """当前模式用不了时，该换成哪个。

    ## 为什么要这么做

        老爸的原话（2026-11-05）：
            「你要检测用户有没有回退嘛，有的话就给他打到有的。」

        场景：用户本来开着「严格防护」，后来把邮箱关了。
            严格防护每拦一次都要发码，码发不出去 → 人卡死。

            这时候界面只是把按钮灰掉，等于把问题丢给用户：
            "你自己想办法"。而他可能根本不知道发生了什么。

        **应该自动帮他换到一个还能用的模式**，
        并且明确告诉他"因为你关了邮箱，我帮你换成了出事可回退"。

    ## 怎么选

        按保护强度从高到低（FALLBACK_ORDER）找第一个能用的。

        ⚠️ 不选"降得最少的那个"，而选"保护最强的那个"——
            同样是从严格防护掉下来，
            能选「全开」（有码有回退）就不选「出事可回退」（只有回退）。

    ## 返回

        能用的模式名。一个都没有就返回空字符串。
    """
    c = caps or capabilities()
    modes = c.get("modes") or {}

    if not any_available(c):
        return ""

    for cand in FALLBACK_ORDER:
        if cand == cur:
            continue        # 当前这个已经判定不可用了，跳过
        if (modes.get(cand) or {}).get("ok"):
            return cand

    # 兜底：万一当前这个其实是能用的（调用方判断错了），保持不动
    return cur if (modes.get(cur) or {}).get("ok") else ""


def explain_fallback(cur: str, new: str, caps: dict | None = None) -> str:
    """为什么自动换了一个模式 —— 给用户看的一句话。"""
    c = caps or capabilities()
    modes = c.get("modes") or {}
    labels = {"strict": "严格防护", "both": "全开", "rollback": "出事可回退"}

    why = str((modes.get(cur) or {}).get("why") or "").strip()
    a = labels.get(cur, cur)
    b = labels.get(new, new)

    if not why:
        return f"原来的「{a}」现在开不了，已经帮你换成「{b}」。"
    return f"「{a}」现在开不了（{why}），已经帮你换成「{b}」。"
