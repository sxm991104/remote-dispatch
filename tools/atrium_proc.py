"""管 atrium 这个进程：起、停、重启。

## 为什么桌面端要管它

    atrium 是「门」—— 聊天软件的消息从它进来。
    但它是**独立进程**，桌面端原来只管配它的配置文件，
    从来不起、也不停。

    结果：
        · 用户改了飞书/钉钉的开关，配置文件变了，atrium 还在按旧的跑
        · 桌面上只能弹一句"要重启 atrium 才生效"，然后就没有然后了
        · 用户得自己去命令行敲 python -m atrium.cli run

    （2026-11-05 老爸的原话：「开关飞书钉钉其实就是重启呀，
     对不对？不重启怎么生效呢？」）

## 三种状态

    没跑        找不到进程
    跑着        心跳文件新鲜（60 秒内写过）
    半死不活    进程还在但心跳不更新（卡住了）

## ⚠️ 停的方式

    先温和地让它在跑完手头的活之后退出，超时再强杀。

    但 atrium 没有"优雅退出"的接口（它是长连接），
    所以实际就是 kill。这没问题：
        · 它收到的消息已经落盘到队列了（atrium/core/queue.py）
        · 明面上的状态在 json 里，不会丢
    唯一会丢的是"正在跟模型对话的那一两秒"——
    那种消息还没写进队列，重启后会丢。
    这个代价可以接受（总比改完配置不生效强）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def _cfg_dir() -> str:
    from tools import userdirs

    return os.path.dirname(userdirs.config_file("x.json"))


def _state_path() -> str:
    from tools import userdirs

    return userdirs.config_file("atrium_proc.json")


def _alive_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".atrium", "alive.json")


# ── 找 atrium 在哪 ──────────────────────────────────────

def find_dir() -> str:
    """atrium 的仓库/安装目录。

    顺序：
        一、用户在界面上填过的（存 atrium_proc.json）
        二、环境变量 ATRIUM_HOME
        三、跟桥并排的目录（开发时的常见布局）
        四、能 import 到就取它的位置
    """
    # 一、存过的
    try:
        with open(_state_path(), encoding="utf-8") as f:
            d = json.load(f)
        p = str(d.get("dir") or "").strip()
        if p and os.path.exists(os.path.join(p, "atrium", "__init__.py")):
            return p
    except (OSError, json.JSONDecodeError):
        pass

    # 二、环境变量
    p = os.environ.get("ATRIUM_HOME", "").strip()
    if p and os.path.exists(os.path.join(p, "atrium", "__init__.py")):
        return p

    # 三、跟桥并排
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (
        os.path.join(os.path.dirname(here), "atrium"),
        os.path.join(os.path.dirname(here), "..", "atrium"),
    ):
        c = os.path.abspath(cand)
        if os.path.exists(os.path.join(c, "atrium", "__init__.py")):
            return c

    # 四、import
    try:
        import atrium  # noqa: F401

        f = getattr(atrium, "__file__", "") or ""
        if f:
            return os.path.dirname(os.path.dirname(os.path.abspath(f)))
    except ImportError:
        pass

    return ""


def remember_dir(path: str) -> tuple[bool, str]:
    """记住 atrium 在哪。"""
    p = (path or "").strip()
    if not p:
        return False, "路径是空的"
    if not os.path.exists(os.path.join(p, "atrium", "__init__.py")):
        return False, f"这个目录里没有 atrium 包：{p}"
    try:
        os.makedirs(_cfg_dir(), exist_ok=True)
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"dir": p}, f, ensure_ascii=False)
        os.replace(tmp, _state_path())
    except OSError as e:
        return False, f"存不住：{e}"
    return True, "记住了"


# ── 看状态 ──────────────────────────────────────────────

def heartbeat() -> dict:
    """读心跳文件。"""
    try:
        with open(_alive_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def is_running(fresh: float = 60.0) -> bool:
    """atrium 现在跑着吗。

    ⚠️ 判据是**心跳新鲜**，不是"进程在不在"。
        进程可能在但卡死了（连不上平台、线程死了），
        那种情况对用户来说就是"没工作"。
    """
    d = heartbeat()
    try:
        ts = float(d.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    return (time.time() - ts) < fresh


def pid() -> int:
    """正在跑的 atrium 的 pid。0 = 没有。"""
    d = heartbeat()
    try:
        return int(d.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def status() -> dict:
    """给界面看的状态。"""
    d = heartbeat()
    running = is_running()
    where = find_dir()
    try:
        started = float(d.get("started") or 0)
    except (TypeError, ValueError):
        started = 0
    try:
        uptime = float(d.get("uptime") or 0)
    except (TypeError, ValueError):
        uptime = 0

    return {
        "running": running,
        "pid": pid(),
        "dir": where,
        "started": started,
        "uptime": int(uptime),
    }


# ── 停 ──────────────────────────────────────────────────

def _pids_of_atrium() -> list[int]:
    """把所有 atrium.cli 的进程找出来。

    ## ⚠️ 为什么不用心跳里的 pid

        心跳里那个是**真解释器**（uv 的 launcher 会再 spawn 一个）。
        launcher 自己也算一个 atrium.cli 进程，只杀一个会留半截。

        而且万一心跳文件是旧的（上次没清干净），那个 pid 可能已经被
        别的程序复用了 —— 照着杀会杀错东西。

    ## ⚠️ 不用 wmic

        新 Windows（24H2 起）已经默认不带 wmic 了。
        这条路在别人机器上直接查不到。

        改用 PowerShell 的 CIM 查询。

    ## ⚠️ 匹配要精确

        命令行里必须**同时**有 "atrium.cli" 和 " run"（带前导空格），
        而且那个进程得是 python 解释器。

        只匹配 "atrium.cli" 的话，会把"正在执行的命令里提到 atrium.cli 的"
        进程也算进来 —— 比如一个 grep、一个编辑器、一次 shell 脚本。
        （实测踩过：一个跑 python -c 的 shell 因为脚本内容里有
          "atrium.cli" 就被当成了 atrium 进程。）
    """
    out: list[int] = []
    ps = (
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Name -match '^python' -and "
        "$_.CommandLine -match 'atrium\\.cli' -and "
        "$_.CommandLine -match '\\brun\\b' } | "
        "ForEach-Object { \"$($_.ProcessId)|\" + $($_.CommandLine) }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=20,
            creationflags=0x08000000,
        )
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            pid_s, cmd = line.split("|", 1)
            if not pid_s.strip().isdigit():
                continue
            low = cmd.lower()
            # 再确认一次：命令行里真的在跑 atrium.cli run
            if "atrium.cli" not in low or " run" not in low:
                continue
            # 排除"命令行里只是提到了这几个字"的情况：
            # 真正的启动命令形如 ... -m atrium.cli run
            if "-m atrium.cli" not in low:
                continue
            out.append(int(pid_s.strip()))
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def stop(timeout: float = 8.0) -> tuple[bool, str]:
    """停掉 atrium。"""
    pids = _pids_of_atrium()
    if not pids and not is_running():
        return True, "atrium 本来就没在跑"

    for p in pids:
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(p)],
                capture_output=True, timeout=20,
                creationflags=0x08000000,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    # 等心跳停止
    end = time.time() + timeout
    while time.time() < end:
        if not is_running():
            return True, "已停"
        time.sleep(0.3)
    return True, "已发出停止指令"


# ── 起 ──────────────────────────────────────────────────

def start() -> tuple[bool, str]:
    """把 atrium 起起来。

    ⚠️ 用当前解释器起。
        桌面端本身就是 python 进程，用 sys.executable 最稳 ——
        用户机器上可能有好几个 python，用 PATH 里的不一定装了 atrium 的依赖。
    """
    where = find_dir()
    if not where:
        return False, (
            "找不到 atrium 在哪。去「通道」页填一下 atrium 的目录"
            "（里面有 atrium/__init__.py 的那个）。"
        )

    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONPATH": where + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }

    try:
        subprocess.Popen(
            [sys.executable, "-m", "atrium.cli", "run"],
            cwd=where, env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=0x08000000 | 0x00000008,   # NO_WINDOW | DETACHED
        )
    except OSError as e:
        return False, f"起不来：{e}"

    return True, "正在启动…"


def restart() -> tuple[bool, str]:
    """重启：先停、再起、等心跳。"""
    ok1, _m1 = stop()
    # 停完等一下，让端口和长连接真断干净
    time.sleep(1.2)

    ok2, m2 = start()
    if not ok2:
        return False, m2

    # 等心跳（最多 25 秒）
    end = time.time() + 25
    t0 = time.time()
    while time.time() < end:
        if is_running(fresh=15):
            return True, f"atrium 已重启（{time.time() - t0:.0f} 秒）"
        time.sleep(0.4)

    return False, "atrium 起来了，但 25 秒内没写心跳 —— 可能连不上平台，去「诊断」看看"
