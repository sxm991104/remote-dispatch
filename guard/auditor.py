"""安全审核员。

## 这是什么

    项目里有三个"角色"：

        前台    聊天窗口里那个，理解用户、派活
        任务端  真动手的那个，改文件、跑命令
        **审核员**  就是这里 —— 只做一件事：看这次改动对不对

    审核员和前两个的根本区别：

        不能对话      没有聊天入口，不接受任何"说明"和"理由"
        没有记忆      每次调用都是一个全新窗口，不看上一次说了什么
        不读项目      不看 README、不看任务描述、不看历史

    它只看**这次改动本身**，然后给出一个判断。

    为什么这样设计：

        一个能对话、有上下文的审查员，可以被说服。
        "我们这是在测试"、"这是朋友让我删的" —— 这类话它听多了就会放行。

        而没有上下文、不能对话的审查员，这些话根本传不进去。
        哪怕有人写一个文件想诱导它，下一次调用它是全新窗口，
        它不认识那个文件。

        **这跟助手的设计是反着来的，是故意的。它像风控，不像助手。**

## 完整流程

    任务端跑命令
        ↓
    影子执行扣住改动
        ↓
    review()
        ├─ skip   审核员没开/没配 → 等于没有这道闸
        ├─ pass   通过 → 落地
        ├─ asking 命中，正等用户填码 → 扣住
        ├─ off    用户已证明身份，窗口内不审 → 落地
        └─ locked 码没来/填错三次 → 扣住，任务端什么都干不了

    命中时：
        生成审核端的码（guard/auditor_code.py，跟任务端的码分开）
        ↓
        发到用户邮箱（复用 guard 的 SMTP 配置）
        ↓
        写一条通知进 outbox（**单向**，前台能读，回不了话）

    用户填码：
        前台把码转给服务端 /api/auditor/verify
        ↓
        服务端验（auditor_code.verify）
        ↓
        对了 → mark_off(duration) → 窗口期内不审

## ⚠️ 单向

    审核员 → 前台     ✅ 写 outbox
    前台 → 审核员     ❌ **代码里一次都没有**

    前台只负责"把用户说的码转给服务端"。
    它不知道码是什么，也改不了审核员的状态。

## ⚠️ 码为什么不能跟任务端共用

    任务端的码   放行**那一个危险动作**
    审核端的码   关掉**审核一段时间**

    共用一个码的话，填了任务端的码，审核也跟着关了 —— 那是错的。
    而且计数会互相连累：一边填错三次，另一边也被锁。

    所以：复用发信通道和"错三次锁死"的逻辑，**码和计数各自独立**。

## 位置

    插在 guard/sandbox.py 的 apply_after_run 里 ——
    影子执行跑完、拿到改动清单之后，**回写到真机之前**。

## 现在的状态

    review() 的流程完整（发码、通知、验码、关窗口），
    _do_review() 也真的调模型了（见 guard/auditor_review.py）。

    真跑过的验证（2026-11-02）：
        删掉权限校验   → block
        只改文案       → pass
        偷偷加网络请求 → block
        整个文件被覆盖 → block（还报出「少了 99%」）

    还没做的：
        · 真实任务链路里没跑过一整轮
          （有单元级验证，没有真派一个任务让它拦）
"""

from __future__ import annotations

import json
import os
import time as _time
import urllib.error

DEFAULT = {
    "enabled": False,       # 默认关。这是要花钱的（每次都调模型）
    "base_url": "",
    "api_key": "",
    "model": "",
    # 审核员挂了怎么办：
    #   "allow"   放行（不能因为它挂了就把用户卡死）
    #   "deny"    拦住（安全优先）
    "on_error": "allow",
    # 触发审核的改动类型：
    #   删除永远审（不可逆）
    #   writes / modifies 可配
    "audit_writes": True,
    "audit_modifies": True,
}


def _cfg_path() -> str:
    # 延迟导入，跟 guard/security.py 一个套路
    from tools import userdirs

    return userdirs.config_file("auditor.json")


def load() -> dict:
    """读配置。没配过就返回默认（enabled=False）。"""
    d = dict(DEFAULT)
    try:
        with open(_cfg_path(), encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in DEFAULT:
                if k in raw:
                    d[k] = raw[k]
    except (OSError, json.JSONDecodeError):
        pass
    return d


def save(cfg: dict) -> bool:
    """存配置。只存认识的键。"""
    clean = {k: cfg.get(k, DEFAULT[k]) for k in DEFAULT}
    p = _cfg_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def is_ready(cfg: dict | None = None) -> tuple[bool, str]:
    """能不能用。返回 (能用, 原因)。"""
    c = cfg if cfg is not None else load()
    if not c.get("enabled"):
        return False, "审核员没开"
    if not (c.get("base_url") and c.get("api_key") and c.get("model")):
        return False, "缺 base_url / api_key / model"
    return True, ""


# ══════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════

def review(changes: dict) -> dict:
    """审一次改动。

    返回
        {
            "verdict":  "pass" | "block" | "skip" | "locked" | "asking" | "off",
            "reasons":  [为什么...],
            "capacity": "这处改动让程序多出了什么能力",
            "rule":     "A".."F" 或 "",
        }

        pass     全部通过
        block    这次拦下了，需要走验证码流程
        skip     审核员没开/没配，等于没有这道闸
        locked   任务端已经锁死（码没来），什么都别干
        asking   正在等用户填码
        off      用户已证明身份，窗口内不审
    """
    # ⚠️ 先看用户选的安全模式。
    #
    #    模式二（出事可回退）不需要审计员 ——
    #    他选的是"别管我，出事退回去"。
    try:
        from guard import security_mode as _sm

        if not _sm.wants("audit"):
            return _ret("skip", ["当前模式不需要审计"])
    except Exception:  # noqa: BLE001
        pass

    cfg = load()
    ok, why = is_ready(cfg)
    if not ok:
        return _ret("skip", [why])

    # 一、先看流程状态：锁死了？还是已经关了？
    from guard import auditor_state as st

    s = st.get()
    if s["state"] == st.STATE_LOCKED:
        return _ret("locked", ["任务端已锁死：审计命中后没拿到验证码"])

    if s["state"] == st.STATE_OFF:
        # 用户已经证明过身份，这段时间内不审
        left = st.remaining()
        return _ret("off", [f"审核已由用户关闭，还剩 {max(0, left) // 60} 分钟"])

    if s["state"] == st.STATE_ASKING:
        return _ret("asking", ["正在等用户填验证码"])

    # 二、真的审一次
    verdict = _do_review(changes, cfg)

    if verdict.get("level") != "block":
        # pass / warn 都放行
        return _ret("pass", [verdict.get("why") or ""], verdict)

    # 三、命中了 → 要码
    #
    # ⚠️ 只有**第一个**命中的人该发码、该通知。
    #
    #    并发的时候（前台一次派 5 个活，每个都被命中），
    #    5 个任务几乎同时走到这里。如果每个都发码、都写通知，
    #    用户会收到 5 条一模一样的"要验证码"，
    #    而他只需要填一个 —— 因为码的作用是"关一段时间"，
    #    不是"放行这一个改动"。
    #
    #    mark_asking 内部有文件锁，保证只有一个能拿到 True。
    reason = verdict.get("why") or "改动命中风险规则"
    if not st.mark_asking(reason):
        # 别人已经在等了，我这里什么都不做
        return _ret("asking", ["已经有人在等验证码了"])

    # 生成（或复用）一个审核端专用的码。
    #
    # ⚠️ 不跟任务端的码共用。见文件开头的说明。
    from guard import auditor_code as ac

    code, is_new = ac.ensure_code()

    # 发信（复用 guard 的 SMTP 配置）。
    #
    # 为什么要发信而不是只在前台说一声：
    #    前台被诱导了，它可以把"要不要码"这件事糊过去；
    #    但码在邮箱里，黑客拿不到。
    sent, send_msg = _send_code(code, is_new, verdict, changes)

    # 单向通告前台。不管信发没发出去，都通告。
    #
    # ⚠️ 通告里不带码。前台能看到的只是"要码了"。
    #    码本身只在邮箱里。
    _notify_front(build_notice(verdict, changes, sent, send_msg))

    return _ret("block", [reason], verdict)


def _ret(verdict: str, reasons: list, detail: dict | None = None) -> dict:
    d = detail or {}
    return {
        "verdict": verdict,
        "reasons": [r for r in reasons if r],
        "capacity": d.get("capacity") or "",
        "rule": d.get("rule") or "",
    }


# ══════════════════════════════════════════════════════
# 发信
# ══════════════════════════════════════════════════════

def _send_code(code: str, is_new: bool, verdict: dict,
               changes: dict) -> tuple[bool, str]:
    """把审核验证码发到用户邮箱。

    复用 guard 的发信配置（guard.json 里的 smtp_*），但**码是另一套**。

    返回 (发出去了没, 说明)。
    """
    try:
        from guard import guard as _guard
    except Exception as e:  # noqa: BLE001
        return False, f"读不到发信配置：{e}"

    # 已经在等确认了，不重复发。
    #
    # ⚠️ 用户可能手慢，或者邮件延迟。
    #    每命中一次就再发一封，他邮箱里会堆一堆码，反而不知道该填哪个。
    if not is_new:
        return True, "码已经发过了（10 分钟内有效）"

    cap = verdict.get("capacity") or "不明"
    why = verdict.get("why") or ""

    files: list[str] = []
    for p in (changes.get("deletes") or [])[:3]:
        files.append(f"删除 {p}")
    for _s, r in (changes.get("modifies") or [])[:3]:
        files.append(f"修改 {r}")
    for _s, r in (changes.get("writes") or [])[:3]:
        files.append(f"新建 {r}")

    body = (
        f"审查员发现一处改动有问题：{why}\n"
        f"这处改动让程序：{cap}\n"
        f"涉及：{'；'.join(files) if files else '（没列出来）'}\n"
        "\n"
        f"验证码：{code}\n"
        "\n"
        "如果这是你自己在干活，填这个码可以关掉审查员一段时间。\n"
        "如果不是 —— 什么都别做，任务端会锁死。\n"
    )

    # 发信走 guard/emailer.py —— 那边只做「把文字发出去」这一件事，
    # 不管内容。码在这个函数里生成、在这个函数里保管。
    from guard import emailer

    try:
        return emailer.send("审核端验证码", body)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════
# 通告前台（单向）
# ══════════════════════════════════════════════════════

def build_notice(verdict: dict, changes: dict,
                 sent: bool = True, send_msg: str = "") -> str:
    """给前台看的通告。

    ⚠️ 这里面**没有码**。

        前台能看到的只是"要码了、因为什么"。
        码本身只在用户邮箱里 —— 前台被诱导也传不出去。

    ⚠️ 也不能有任何"可以回复审核员"的暗示。
        前台拿到这个，只做一件事：告诉用户。
    """
    files: list[str] = []
    for p in (changes.get("deletes") or [])[:5]:
        files.append(f"删除 {p}")
    for _s, r in (changes.get("modifies") or [])[:5]:
        files.append(f"修改 {r}")
    for _s, r in (changes.get("writes") or [])[:5]:
        files.append(f"新建 {r}")

    cap = verdict.get("capacity") or "不明"
    why = verdict.get("why") or ""

    head = (
        "【需要验证码】\n"
        f"审查员发现一处改动有问题：{why}\n"
        f"这处改动让程序：{cap}\n"
        f"涉及：{'；'.join(files) if files else '（没列出来）'}"
    )

    if not sent:
        head += f"\n\n⚠️ 但验证码没发出去（{send_msg}）。"
        head += "去「设置 → 远程验证」把邮箱配好。"

    return head + (
        "\n\n"
        "**如果这是你本人在干活**，回复邮箱收到的 6 位验证码，"
        "再选一个时长（1 小时 / 4 小时 / 今天）——"
        "这段时间内审查员不再拦。\n"
        "**如果不是你要的**，什么都别回 —— 任务端会锁死。"
    )


def _notify_front(text: str) -> None:
    """写一条通知进 outbox，前台会看到。

    ⚠️ 这是**单向**的：

        审核员 → outbox → 前台 → 用户

        写进 outbox 之后，审核员就不再管这件事了。
        没有"回信"这个方向 —— 前台也给不了审核员任何东西。

    ⚠️ 为什么不能直接调 push 插件

        那是另一个进程（AstrBot），而且我们只该"投递一封通知"，
        不该去猜它现在连着哪个平台、有什么会话。
        outbox 是现成的通道，push 插件会来读。

        这也顺带满足"单向"：我们会写，但读不到任何回信。
    """
    try:
        # 跟 core/bridge.py 用同一个文件、同一种格式。
        #
        # 不能自己拼路径 —— core/bridge.py 的 BRIDGE_DIR 有它自己的算法
        # （打包成 exe、从源码跑、被插件调用，三种情况不一样）。
        # 直接 import 它拿常量，比自己算可靠。
        from core import bridge as _bridge

        note = {
            # ⚠️ id 不能固定。
            #
            #    推送那边用 id 去重：推过一次就标 delivered=True。
            #    id 固定的话，第一条送达之后，以后所有的审计通知
            #    都会被当成"已送达"，推不出去。
            #
            #    加毫秒时间戳，每次都是新 id。
            "id": f"audit-{int(_time.time() * 1000)}",
            "name": "安全审核员",
            "profile": "",
            "status": "need_code",
            "ok": False,
            "task": "文件改动审核",
            "result": "",
            "error": "",
            "seconds": None,
            "finished_at": "",
            "files": [],
            "files_skipped": [],
            "delivered": False,
            # 推给前台时，这一段会被单独拎出来。
            # push 插件看到 kind=audit 就不按"任务完成"的模板发。
            "kind": "audit",
            "text": text,
        }
        with open(_bridge.OUTBOX, "a", encoding="utf-8") as f:
            f.write(json.dumps(note, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        # 通告发不出去不是致命问题：改动仍然被扣着，
        # 用户下次打开界面也能看到。
        pass


# ══════════════════════════════════════════════════════
# 真的调模型
# ══════════════════════════════════════════════════════

def _do_review(changes: dict, cfg: dict) -> dict:
    """真的调模型审一次。

    返回 {level, why, capacity, rule}

        level  "pass" / "warn" / "block"

    ⚠️ 送进去的只有改动本身。
        不送任务描述、项目说明、上一次的结论、用户说过的话 ——
        这些都会让它被「解释」，而解释正是我们要防的。

    ⚠️ 模型挂了 / 返回乱了怎么办：按 cfg["on_error"]
        allow   放行（不能因为它挂了就把用户卡死）  ← 默认
        "deny"   拦住（安全优先）
    """
    from guard import auditor_prompt as pr
    from guard import auditor_review as rv

    def _fail(msg: str) -> dict:
        # 审核员自己出问题了
        if cfg.get("on_error") == "deny":
            return {
                "level": "block",
                "why": f"审核员出问题了，按安全优先拦住：{msg}",
                "capacity": "不明",
                "rule": "",
            }
        return {"level": "pass", "why": f"审核员出问题，放行：{msg}",
                "capacity": "", "rule": ""}

    try:
        text, meta = rv.build_changes_text(changes)
    except Exception as e:  # noqa: BLE001
        return _fail(f"整理改动失败：{type(e).__name__}")

    if not text.strip():
        # 没有实际内容要审（比如只动了空文件）
        return {"level": "pass", "why": "没有实质改动", "capacity": "", "rule": ""}

    user = pr.build_user_prompt(text, meta)

    try:
        raw = rv._call_model(cfg, pr.SYSTEM, user)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return _fail(f"接口返回 {e.code}：{body}")
    except Exception as e:  # noqa: BLE001
        return _fail(f"{type(e).__name__}：{e}")

    d = rv._parse_json(raw)
    if not d:
        return _fail(f"返回的不是 JSON：{raw[:120]}")

    level = str(d.get("level") or "pass").lower()
    if level not in ("pass", "warn", "block"):
        level = "warn"   # 看不懂的档位，当成可疑而不是安全

    return {
        "level": level,
        "why": str(d.get("why") or ""),
        "capacity": str(d.get("capacity") or ""),
        "rule": str(d.get("rule") or ""),
    }
