"""审计流程的状态。跨进程落盘。

## 四个状态

    PASS      没命中，什么都没发生
    ASKING    命中了，发了码，正等用户填 + 选时长
    OFF       用户证明过身份，在 <到期时间> 之前不审
    LOCKED    码没来（或填错），任务端锁死

## ⚠️ OFF 是**带到期时间**的，不是永久的

    用户填码的时候会顺便选一个时长（他按自己的工作时间判断）：

        1 小时 / 4 小时 / 今天

    时间一到，自动回到 PASS。

    **然后下一次命中，就又要码。**

        不是"明天又要码"——是"窗口过期了又要码"。

        9:00   命中 → 发码 → 选「关 4 小时」
        13:00  到期 → 回到 PASS
        13:05  又命中 → **又要码**

    这样：
        · 用户按自己的节奏来，不会被天天问
        · 但也不会"填一次，永远不问"

## ⚠️ 为什么敲盘

    任务端和接口服务是两个进程。
    审计在一个进程里命中，锁死/关掉的状态另一个进程也要看得见；
    不然一边锁死、另一边照跑。

    写法跟 guard/lock_state.json 那套一样：
        小文件 + 原子替换（写 tmp 再 os.replace）

## ⚠️ 为什么"只问一次就够"

    见 guard/auditor_flow.py。

    码不是"某个改动的通行证"，是"你就是户主、并且决定关多久"的证明。
    所以这里的状态是**全局**的，不分任务 ——
    这也就解掉了并发问题（5 个任务同时命中，只要一个码）。
"""

from __future__ import annotations

import json
import os
import time

STATE_PASS = "pass"
STATE_ASKING = "asking"
STATE_OFF = "off"
STATE_LOCKED = "locked"

# 用户能选的时长（秒）。前端给这三个选项。
DURATIONS = {
    "1h": 3600,
    "4h": 4 * 3600,
    "today": None,      # 到当天 23:59:59
}


def _path() -> str:
    from tools import userdirs

    return userdirs.config_file("audit_state.json")


def acquire(timeout: float = 5.0):
    """拿一个跨进程 + 跨线程的排他锁。

    ⚠️ 为什么需要这个

        并发命中时，5 个任务几乎同时检查状态 ——
        它们都看到 STATE_PASS，于是**都往下走**：
        都调 ensure_code、都写通知。

        结果：outbox 里 5 条一模一样的"要验证码"，
        用户收到 5 条（各带一个唯一 id，推送那边不会合并）。

        而码只有一个（ensure_code 会复用），
        所以用户填了那个码，5 个任务全放行 ——
        行为是对的，但**通知发重了**。

    ⚠️ 为什么不用 threading.Lock

        任务端是多进程的（每个 worker 一个进程）。
        线程锁只管一个进程里的线程。

    实现跟 guard/count_store.py 同一套：
        POSIX    fcntl.flock
        Windows  msvcrt.locking

    用法（上下文管理器）：
        with acquire():
            ... 检查状态、改状态 ...
    """
    import contextlib

    p = _path() + ".lock"

    @contextlib.contextmanager
    def _ctx():
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fh = open(p, "a+b")
        try:
            # ⚠️ 先 seek(0) 并保证至少有 1 字节。
            #    msvcrt.locking 从**当前位置**开始锁，
            #    "a+b" 打开后指针在 EOF，不 seek 就锁不到东西。
            fh.seek(0)
            if fh.read(1) == b"":
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)

            got = False
            try:
                import fcntl

                # POSIX
                import time as _t

                deadline = _t.time() + timeout
                while _t.time() < deadline:
                    try:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        got = True
                        break
                    except OSError:
                        _t.sleep(0.02)
            except ImportError:
                import msvcrt

                import time as _t

                deadline = _t.time() + timeout
                while _t.time() < deadline:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        got = True
                        break
                    except OSError:
                        _t.sleep(0.02)

            try:
                yield got
            finally:
                if got:
                    try:
                        import fcntl

                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    except ImportError:
                        import msvcrt

                        fh.seek(0)
                        try:
                            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
        finally:
            fh.close()

    return _ctx()


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


def _end_of_today() -> float:
    """今天 23:59:59 的时间戳。"""
    import datetime as _dt

    now = _dt.datetime.now()
    end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return end.timestamp()


def get() -> dict:
    """当前状态。

    ⚠️ OFF 到期会自动变回 PASS —— 读的时候判断，不用后台定时器。

        理由：定时器要多一个常驻线程，而且进程重启就丢了。
        读的时候顺手看一眼到期时间，最简单也最可靠。
    """
    d = _load()
    state = d.get("state") or STATE_PASS
    until = float(d.get("until") or 0)

    if state == STATE_OFF:
        # ⚠️ 不要写 `if until and time.time() >= until`。
        #
        #    until 为 0 时那个判断整个短路，于是"没有到期时间"的 off
        #    会被当成"还早着呢"，永远不恢复。
        #
        #    实测踩到：手动把 until 改成 0 想模拟过期，
        #    结果状态一直卡在 off，审核员再也不拦了 ——
        #    这正是"审计形同虚设"的那种情况。
        #
        #    正确做法：没有到期时间（0）就当已经过期。
        if not until or time.time() >= until:
            # 窗口过了 → 审核恢复
            _save({"state": STATE_PASS, "since": time.time(),
                   "why": "关掉的时长到了，审核恢复"})
            return {"state": STATE_PASS, "since": time.time(),
                    "until": 0, "why": "关掉的时长到了，审核恢复"}

    return {
        "state": state,
        "since": d.get("since") or 0,
        "until": until,
        "why": d.get("why") or "",
    }


def mark_asking(why: str = "") -> bool:
    """命中了，正等验证码 + 用户选时长。

    ⚠️ 返回**这次是不是第一个**。

        并发命中时，5 个任务同时走到这里。
        只有第一个该发码、该通知；其余的直接返回 asking。

        这个"谁先"的判断必须和状态写入是原子的 ——
        所以里面套了文件锁。

    返回 True  = 我是第一个，你去发码、去通知
    返回 False = 已经有人在等了，你什么都别做
    """
    with acquire() as got:
        cur = _load().get("state") or STATE_PASS

        # 已经有人在等了 / 已经锁死了 → 不是第一个
        if cur in (STATE_ASKING, STATE_LOCKED):
            return False

        _save({"state": STATE_ASKING, "since": time.time(),
               "until": 0, "why": why})
        return True


def mark_off(duration: str = "1h", why: str = "") -> float:
    """用户证明过身份，关掉一段时间。返回到期时间戳。"""
    secs = DURATIONS.get(duration)
    until = _end_of_today() if secs is None else time.time() + secs
    _save({
        "state": STATE_OFF,
        "since": time.time(),
        "until": until,
        "why": why or f"用户验证通过，关掉 {duration}",
    })
    return until


def mark_locked(why: str = "审计命中后没拿到验证码") -> None:
    """锁死任务端。"""
    _save({"state": STATE_LOCKED, "since": time.time(), "until": 0, "why": why})


def reset(why: str = "手动重置") -> None:
    """回到正常状态。"""
    _save({"state": STATE_PASS, "since": time.time(), "until": 0, "why": why})


def unlock_off(why: str = "") -> None:
    """用户主动关掉（不走时长，直接回 PASS，下次命中又要码）。"""
    reset(why or "用户主动关掉")


def is_locked() -> bool:
    return get()["state"] == STATE_LOCKED


def is_off() -> bool:
    return get()["state"] == STATE_OFF


def remaining() -> int:
    """OFF 状态还剩多少秒。不是 OFF 返回 0。"""
    s = get()
    if s["state"] != STATE_OFF:
        return 0
    return max(0, int(s["until"] - time.time()))
