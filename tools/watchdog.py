"""服务守护 —— 只在被叫的时候才醒。

为什么需要它
    服务挂了要有人拉。但"要有人拉"这件事本身有个圈：
    如果拉它的人也在服务里，那服务一死，拉它的人也死了。

    所以得有一个**不在被守护进程树下面**的独立东西盯着。

它现在守护谁
    **atrium** —— 那个接聊天平台、当前台的门。

    以前的版本守护的是 AstrBot，那套已经废弃了（前台搬去了 atrium）。
    守护逻辑本身没变，只是换了个目标。

为什么不是计划任务轮询
    计划任务是"每 N 分钟查一次"——没病也去敲门，纯浪费。
    而且真挂的时候，要等下一个周期才发现。

    这里用 Windows 的目录变更通知（ReadDirectoryChangesW）：
    内核在文件变化时把事件推过来，**没有事件时这个进程一个 CPU 周期都不占**。

    另外有一个低频轮询兜底（默认 30 秒看一次心跳），
    因为"它自己崩了"这件事没人会写请求文件。

它监听什么
    <桥目录>/restart.request

    其他程序（比如桌面端、任务端）要重启服务时，只做一件事：
    往这个文件里写一行。剩下的交给这里。

它做什么
    1. 看到 restart.request 出现/变化
    2. 读里面的内容（记下是谁请求的、什么理由）
    3. 杀掉服务整棵进程树
    4. 重新拉起
    5. 等心跳恢复，确认成功
    6. 把结果写到 restart.result，删掉请求文件

怎么判断"活着"
    atrium 每 10 秒写一次 ~/.atrium/alive.json（见 atrium/core/beat.py）。

    这里读那个文件：
        超过 60 秒没更新 → 判定挂了

    为什么不查端口：atrium 不监听端口（它主动连平台出去），
    没有端口可以查。

防护（这些不是可选项）
    · **冷却期**：一次重启后 5 分钟内不再重启第二次。
      以前因为重启太频繁被平台风控过，这个必须挡住。
    · **连续失败上限**：连续失败 3 次就停手，写个 .stuck 文件，
      不再自动重试。免得把机器搞成"起了就崩、崩了再起"的死循环。
    · **不乱杀**：只杀命令行里带 "atrium.cli" 的 python 进程。
      用户可能还开着别的 Python 程序，那些不能碰。

用法
    pythonw tools/watchdog.py            ← 无窗口跑（推荐）
    python tools/watchdog.py             ← 带控制台，调试时用

配置（环境变量）
    DSH_WATCHDOG_CMD         要跑的命令（默认 <当前python> -m atrium.cli run）
    DSH_WATCHDOG_CWD         在哪个目录跑
    DSH_WATCHDOG_DEAD_AFTER  心跳多久没更新算挂（默认 60 秒）
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes
from datetime import datetime

# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------

BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 请求重启：别的程序往这里写一行
REQUEST = os.path.join(BRIDGE_DIR, "restart.request")

#: 重启结果：这里读
RESULT = os.path.join(BRIDGE_DIR, "restart.result")

#: 连续失败标志
STUCK = os.path.join(BRIDGE_DIR, "restart.stuck")

#: 叫它退出：桌面端关掉时写这个文件
#:
#: ⚠️ 为什么用文件而不是杀进程：
#:    强杀（TerminateProcess）不给它收尾的机会 ——
#:    正在重启服务的时候被砍掉，服务就停在半路上，
#:    而且 restart.result 不会写，桌面端会一直以为"还在重启"。
#:
#:    写个文件让它自己走完当前动作再退，干净。
STOP = os.path.join(BRIDGE_DIR, "watchdog.stop")

#: 看门狗自己的心跳：桌面端靠它判断"是不是已经有一个在跑"
ALIVE = os.path.join(BRIDGE_DIR, "watchdog.alive")

# ── 被守护的东西 ──

# 默认用当前解释器跑 atrium。
# ⚠️ 用 sys.executable 而不是写死路径 ——
#    用户在哪个 Python 里装了依赖，就用哪个跑。
_SELF = os.path.realpath(sys.executable)
WATCH_CMD = (os.environ.get("DSH_WATCHDOG_CMD")
             or f'"{_SELF}" -m atrium.cli run')

# ⚠️ atrium 的代码在哪。
#
#    看门狗通常跟 remote-dispatch 放在一起，
#    而 atrium 是另一个仓库 —— 从这儿跑不起来（找不到模块）。
#
#    所以起进程时要带上 PYTHONPATH。
#
#    默认取 DSH_ATRIUM_DIR，或者跟 remote-dispatch 平级的 ../atrium。
_ATRIUM_DIR = (os.environ.get("DSH_ATRIUM_DIR")
               or os.path.join(os.path.dirname(BRIDGE_DIR), "atrium"))

WATCH_CWD = os.environ.get("DSH_WATCHDOG_CWD") or ""

# ── 判活的阈值 ──

#: 心跳多久没更新算挂
DEAD_AFTER = float(os.environ.get("DSH_WATCHDOG_DEAD_AFTER") or 60)

#: 多久自己看一次心跳
CHECK_EVERY = float(os.environ.get("DSH_WATCHDOG_PROBE") or 30)

#: 起来时等多久算成功
READY_TIMEOUT = 120.0

# ── 防护 ──

#: 一次重启后多久内不再重启（秒）
COOLDOWN = 300.0

#: 连续失败几次就停手
MAX_FAILS = 3


def _log(msg: str) -> None:
    """写一行日志。

    ⚠️ 用 append + flush，不用 logging。
       这东西可能跑在 pythonw 下，没有控制台，
       日志文件是唯一能看到它干了什么的地方。
    """
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with open(os.path.join(BRIDGE_DIR, "watchdog.log"), "a",
                  encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------
# 判活
# ----------------------------------------------------------------------

def _alive_path() -> str:
    """atrium 的心跳文件在哪。"""
    return os.path.join(os.path.expanduser("~"), ".atrium", "alive.json")


def _heartbeat_age():
    """心跳多久没更新了。None 表示没有心跳文件。"""
    try:
        with open(_alive_path(), encoding="utf-8") as f:
            d = json.load(f)
        return max(0.0, time.time() - float(d.get("ts") or 0))
    except (OSError, ValueError, TypeError):
        return None


def _is_alive() -> bool:
    """它活着吗。"""
    age = _heartbeat_age()
    return age is not None and age <= DEAD_AFTER


def _wait_alive(timeout: float = READY_TIMEOUT) -> bool:
    """等它活过来。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _is_alive():
            return True
        time.sleep(1.0)
    return False


# ----------------------------------------------------------------------
# 杀掉 / 拉起
# ----------------------------------------------------------------------

def _kill_tree() -> bool:
    """杀掉被守护进程的整棵进程树。

    ⚠️ 只杀命令行里带 "atrium.cli" 的 python 进程。
       不能乱杀 —— 用户可能还开着别的 Python 程序。
    """
    try:
        ps = (
            'Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | '
            'Where-Object { $_.Name -match "^python" -and '
            '$_.CommandLine -match "atrium.cli" } | '
            'ForEach-Object { $_.ProcessId }'
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        pids = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    except Exception as e:  # noqa: BLE001
        _log(f"查进程失败：{type(e).__name__}: {e}")
        return False

    if not pids:
        _log("没有找到在跑的 atrium 进程")
        return True

    for pid in pids:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", pid],
                capture_output=True, timeout=20,
            )
            _log(f"杀掉了 pid={pid}")
        except Exception as e:  # noqa: BLE001
            _log(f"杀 pid={pid} 失败：{e}")

    # 等它们真死
    time.sleep(2.0)
    return not _is_alive()


def _start() -> bool:
    """拉起它。

    ⚠️ 用 CreateNoWindow，不弹黑框。
       用户可能双击看门狗来的，不该看见一堆窗口。
    """
    if not WATCH_CMD.strip():
        _log("没配 DSH_WATCHDOG_CMD，不知道拉起什么")
        return False

    # ⚠️ 带上 PYTHONPATH，让它能在别处找到 atrium。
    #
    #    不加的话：看门狗在 remote-dispatch 目录下起
    #    `python -m atrium.cli run`，直接 ModuleNotFoundError。
    env = dict(os.environ)
    if _ATRIUM_DIR and os.path.isdir(_ATRIUM_DIR):
        old = env.get("PYTHONPATH") or ""
        parts = [p for p in old.split(os.pathsep) if p]
        if _ATRIUM_DIR not in parts:
            parts.insert(0, _ATRIUM_DIR)
        env["PYTHONPATH"] = os.pathsep.join(parts)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")

    try:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS

        subprocess.Popen(
            WATCH_CMD,
            shell=True,
            cwd=WATCH_CWD or None,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
        _log(f"拉起了：{WATCH_CMD}")
        if _ATRIUM_DIR and os.path.isdir(_ATRIUM_DIR):
            _log(f"  PYTHONPATH 加了 {_ATRIUM_DIR}")
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"拉起失败：{type(e).__name__}: {e}")
        return False


# ----------------------------------------------------------------------
# 冷却 / 连续失败
# ----------------------------------------------------------------------

_last_restart = 0.0
_fails = 0
_lock = threading.Lock()


def _in_cooldown() -> bool:
    return (time.time() - _last_restart) < COOLDOWN


def _note_fail() -> bool:
    """记一次失败。返回 True 表示已经到上限，别再试了。"""
    global _fails

    with _lock:
        _fails += 1
        n = _fails

    if n >= MAX_FAILS:
        try:
            with open(STUCK, "w", encoding="utf-8") as f:
                f.write(f"连续失败 {n} 次，停手了。\n"
                        f"{datetime.now().isoformat()}\n")
        except OSError:
            pass
        _log(f"连续失败 {n} 次，写 .stuck 停手")
        return True

    return False


def _note_ok() -> None:
    global _fails

    with _lock:
        _fails = 0

    try:
        os.remove(STUCK)
    except OSError:
        pass


# ----------------------------------------------------------------------
# 重启
# ----------------------------------------------------------------------

def _do_restart(reason: str, requester: str = "") -> bool:
    """重启一次，返回成功没有。"""
    global _last_restart

    if os.path.exists(STUCK) and not requester:
        # 手动请求可以绕过 .stuck，自动的绕不过
        _log("有 .stuck，自动重启跳过")
        return False

    if _in_cooldown() and not requester:
        left = int(COOLDOWN - (time.time() - _last_restart))
        _log(f"冷却中（还有 {left} 秒），跳过")
        return False

    _last_restart = time.time()

    # ⚠️ 空字符串要显示成"看门狗自己"，
    #    但 requester 是空就是空 —— 别在这里改它的语义。
    shown = requester or "看门狗自己"
    _log(f"开始重启：{reason}（请求方：{shown}）")

    if _is_alive():
        _log("它还在跑，先杀掉")
        if not _kill_tree():
            _log("杀不掉")
            _note_fail()
            return False

    if not _start():
        _log("拉不起来")
        _note_fail()
        return False

    _log("等它活过来……")
    if _wait_alive():
        _log("重启成功")
        _note_ok()
        return True

    _log("等超时了，没活过来")
    _note_fail()
    return False


def _handle_request() -> None:
    """处理一次重启请求。"""
    # ⚠️ global 必须放在函数开头。
    #    放在 if 里面虽然语法上能过，
    #    但读的人会以为只有那个分支才改全局。
    global _last_restart

    try:
        with open(REQUEST, encoding="utf-8") as f:
            raw = f.read().strip()
    except OSError:
        raw = ""

    who = ""
    reason = "外部请求"
    force = False
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                who = str(d.get("who") or "")
                reason = str(d.get("reason") or reason)
                force = bool(d.get("force"))
        except json.JSONDecodeError:
            reason = raw[:100]

    # ⚠️ force=True 绕过冷却和 .stuck。
    #
    #    为什么要这个：
    #        冷却期是防"反复重启被风控"的，
    #        但用户手动点重启的时候，他知道自己在干什么 ——
    #        不该被冷却挡住。
    #
    #    实测（2026-11-04）：测试时刚重启完又崩了，
    #    冷却期让看门狗干看着不动，得等 5 分钟。
    if force:
        _log(f"强制重启（绕过冷却）：{reason}（请求方：{who or '手动'}）")
        _last_restart = 0.0
        try:
            os.remove(STUCK)
        except OSError:
            pass

    # ⚠️ 请求方要区分清楚：
    #    有 who 就用 who；
    #    强制但没写 who，说明是手动点的；
    #    都没有才是"看门狗自己发现的"。
    #
    #    以前这里传的是 `who or "手动"`，
    #    但 _do_restart 里又判断 truthy，
    #    结果日志里印成"看门狗自己"，看不出是谁要重启。
    requester = who or ("手动" if force else "")

    ok = _do_restart(reason, requester)

    try:
        with open(RESULT, "w", encoding="utf-8") as f:
            json.dump({
                "ok": ok,
                "at": datetime.now().isoformat(timespec="seconds"),
                "reason": reason,
            }, f, ensure_ascii=False)
    except OSError:
        pass

    try:
        os.remove(REQUEST)
    except OSError:
        pass


# ----------------------------------------------------------------------
# 目录监听（Windows）
# ----------------------------------------------------------------------

FILE_NOTIFY_CHANGE_LAST_WRITE = 0x10
FILE_NOTIFY_CHANGE_FILE_NAME = 0x01


def _watch_dir_loop(stop: threading.Event) -> None:
    """盯 restart.request 的变化。

    ⚠️ 这是 Windows 专有的。起不来就靠轮询兜底。
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        CreateFileW = kernel32.CreateFileW
        CreateFileW.restype = wintypes.HANDLE
        CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
            wintypes.HANDLE,
        ]

        handle = CreateFileW(
            BRIDGE_DIR,
            0x0001,          # FILE_LIST_DIRECTORY
            0x0007,          # SHARE_READ | SHARE_WRITE | SHARE_DELETE
            None,
            3,               # OPEN_EXISTING
            0x02000000,      # FILE_FLAG_BACKUP_SEMANTICS
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            raise OSError(ctypes.get_last_error(), "CreateFileW 失败")

        buf = ctypes.create_string_buffer(4096)
        returned = wintypes.DWORD()

        ReadDirectoryChangesW = kernel32.ReadDirectoryChangesW
        ReadDirectoryChangesW.restype = wintypes.BOOL
        ReadDirectoryChangesW.argtypes = [
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            wintypes.BOOL, wintypes.DWORD, wintypes.LPDWORD,
            wintypes.LPDWORD, wintypes.LPVOID,
        ]

        _log("目录监听起来（等 restart.request）")

        while not stop.is_set():
            ok = ReadDirectoryChangesW(
                handle,
                buf,
                len(buf),
                False,
                FILE_NOTIFY_CHANGE_LAST_WRITE | FILE_NOTIFY_CHANGE_FILE_NAME,
                ctypes.byref(returned),
                None,
                None,
            )
            if not ok:
                _log("ReadDirectoryChangesW 失败，靠轮询兜底")
                return

            if os.path.exists(REQUEST):
                _handle_request()

    except Exception as e:  # noqa: BLE001
        _log(f"目录监听起不来（{type(e).__name__}: {e}），靠轮询兜底")


def _poll_loop(stop: threading.Event) -> None:
    """兜底：定期看两眼。

    看两件事：
        一、有没有 restart.request
        二、它是不是自己崩了
    """
    _log(f"轮询模式：每 {int(CHECK_EVERY)} 秒看一次")

    while not stop.is_set():
        # ⚠️ 心跳写在最前面，每圈都写。
        #
        #    桌面端靠这个文件判断"看门狗是不是已经在跑" ——
        #    有它而且够新，就不再起第二个。
        #    见 start_desktop.pyw 的 _start_watchdog。
        _touch_alive()

        stop.wait(CHECK_EVERY)
        if stop.is_set():
            break

        try:
            if os.path.exists(REQUEST):
                _handle_request()
                continue

            if not _is_alive():
                age = _heartbeat_age()
                why = "没有心跳" if age is None else f"心跳 {age:.0f} 秒没更新"
                _log(f"发现它不活了（{why}），自动拉起")
                _do_restart(why, "")
        except Exception:  # noqa: BLE001
            _log("轮询出错：\n" + traceback.format_exc())


def _touch_alive() -> None:
    """留个"我还活着"的记号。

    ⚠️ 只在内存里更新 mtime 也行，但写文件最省事且跨进程可见。
       失败就算了 —— 这只是给判重用的，不能因为它挂了看门狗就停。
    """
    try:
        with open(ALIVE, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError:
        pass


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------

def main() -> int:
    _log("=" * 50)
    _log("看门狗启动")
    _log(f"守护命令：{WATCH_CMD}")
    _log(f"判活阈值：心跳 {int(DEAD_AFTER)} 秒不更新算挂")
    _log(f"冷却期：{int(COOLDOWN)} 秒；连续失败 {MAX_FAILS} 次停手")

    # ⚠️ 启动时先把停止文件清掉。
    #
    #    不清的话，上次桌面端关了写了这个文件、进程被强杀没来得及删，
    #    这次一起来就看见它，立刻退出 —— 表现为"看门狗怎么都起不来"。
    try:
        if os.path.exists(STOP):
            os.remove(STOP)
    except OSError:
        pass

    if os.path.exists(STUCK):
        _log("⚠️ 有 .stuck 文件 —— 之前连续失败过。"
             "手动重启请求仍然有效，自动拉起不会做。")

    # 起来先看一眼
    if not _is_alive():
        age = _heartbeat_age()
        _log("启动时发现它没在跑"
             + ("（没有心跳）" if age is None else f"（心跳 {age:.0f} 秒前）")
             + "，先拉起")
        if _start() and _wait_alive():
            _log("起来了")
            _note_ok()
        else:
            _log("没起来")
            _note_fail()
    else:
        _log("它已经在跑，继续盯着")

    stop = threading.Event()

    # 目录监听（主线）
    t = threading.Thread(target=_watch_dir_loop, args=(stop,), daemon=True)
    t.start()

    # 轮询（兜底 + 自己崩了也能发现）
    t2 = threading.Thread(target=_poll_loop, args=(stop,), daemon=True)
    t2.start()

    try:
        while True:
            time.sleep(1.0)
            # 桌面端叫它退：写完这一圈就走
            if os.path.exists(STOP):
                _log("看到停止标记，退出")
                try:
                    os.remove(STOP)
                except OSError:
                    pass
                stop.set()
                break
    except KeyboardInterrupt:
        _log("收到中断，退出")
        stop.set()

    # ⚠️ 走的时候把心跳删掉。
    #
    #    不删的话，桌面端下次启动去看 watchdog.alive，
    #    发现"30 秒内还有更新"（其实是我退出前最后写的那次），
    #    以为已经有看门狗在跑，就不再起 —— 结果是这一趟没有守护。
    try:
        if os.path.exists(ALIVE):
            os.remove(ALIVE)
    except OSError:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
