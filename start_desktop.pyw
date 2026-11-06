"""桌面端的一键启动器。

为什么要多这一层
    桌面端其实是两件事：接口服务 + 界面。
    直接给界面脚本建快捷方式的话，接口服务没人起，
    界面上全是"连不上后台"。

    这个脚本负责：
        1. 看看接口服务起了没，没起就先起它
        2. 等它真的能响应了
        3. 再开界面

    用户双击快捷方式，只看到一个窗口，背后两件事都办好了。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
API_PORT = int(os.environ.get("DSH_BRIDGE_PORT") or 8790)

# ⚠️ 代码结构改过（v1.3.3）：入口文件挪进了子目录。
#
#    原来在根目录：
#        api_server.py
#        desktop_qt.py
#
#    现在：
#        server/api_server.py
#        desktop/app.py
#
#    这两个路径不改，双击快捷方式就什么都没发生——
#    pythonw 下没有控制台，报错也看不见，看着像"点了没反应"。
API_PY = os.path.join(HERE, "server", "api_server.py")
UI_PY = os.path.join(HERE, "desktop", "app.py")


def port_ok(port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _missing() -> str:
    """哪个入口文件找不到。为空表示都在。

    这一步是为了"报错看得见"——
    双击启动没有控制台，缺文件时用户只会觉得"没反应"。
    """
    for p in (API_PY, UI_PY):
        if not os.path.exists(p):
            return p
    return ""


def _say(msg: str) -> None:
    """有控制台就打印，没有就弹框。

    ⚠️ 双击启动时（pythonw）没有控制台 ——
        这时候 print 等于什么都没做，用户只看到"点了没反应"。

        踩过：用户双击桌面快捷方式，窗口一直不出来，
        也没任何提示。查了半天才发现在 import PySide6 就崩了，
        而 pythonw 把那段 traceback 直接丢了。
    """
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, msg, "dsh-bridge", 0x10)
    except Exception:  # noqa: BLE001
        print(msg)


def _pick_python() -> str:
    """挑一个能跑界面的解释器。

    ⚠️ 为什么需要这个

        双击 .pyw 走的是系统关联（C:\\WINDOWS\\pyw.exe），
        它用的是 **WindowsApps 里的那个 Python** ——
        可能是微软商店的占位程序，也可能是另一个没装 PySide6 的。

        结果：窗口还没出来就崩在 import PySide6，
        而 pythonw 没有控制台，报错看不见。

    顺序：
        一、当前解释器（如果是用 pythonw 直接双击，就是它）
        二、同目录的 python.exe
        三、PATH 里的 python
        四、常见位置（LocalAppData / Program Files）

    返回 "" 表示一个都没有。
    """
    import shutil
    import subprocess

    def has_pyside(exe: str) -> bool:
        if not exe or not os.path.exists(exe):
            return False
        # 用 -c 探一下，不 import 界面本体（那个很重）
        try:
            r = subprocess.run(
                [exe, "-c", "import PySide6; print(1)"],
                capture_output=True, timeout=10,
                creationflags=0x08000000,   # NO_WINDOW
            )
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def cache_file() -> str:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "dsh-bridge", "desktop_python.txt")

    def cached() -> str:
        try:
            with open(cache_file(), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def remember(exe: str) -> None:
        try:
            os.makedirs(os.path.dirname(cache_file()), exist_ok=True)
            with open(cache_file(), "w", encoding="utf-8") as f:
                f.write(exe)
        except OSError:
            pass

    # 零、上次找到的那个。
    #
    #    每次双击都要把下面四个位置挨个探一遍，每个都要起一个
    #    python 子进程 import PySide6（1 秒起）——等于每次开程序
    #    先白等好几秒。上次已经验证能用的，这次直接用；
    #    再探一次确认它没被卸掉就够了。
    c = cached()
    if c and has_pyside(c):
        return c

    # 一、当前这个
    if has_pyside(sys.executable):
        remember(sys.executable)
        return sys.executable

    # 二、同目录的 python.exe（pythonw 旁边通常有一个）
    d = os.path.dirname(sys.executable)
    for name in ("python.exe", "pythonw.exe"):
        p = os.path.join(d, name)
        if has_pyside(p):
            remember(p)
            return p

    # 三、PATH
    for name in ("python", "python3"):
        p = shutil.which(name)
        if has_pyside(p):
            remember(p)
            return p

    # 四、常见位置
    import glob

    pats = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Programs", "Python", "Python3*", "python.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""),
                     "Python3*", "python.exe"),
        os.path.join(os.environ.get("USERPROFILE", ""),
                     "AppData", "Roaming", "uv", "tools",
                     "*", "Scripts", "python.exe"),
    ]
    for pat in pats:
        for p in glob.glob(pat):
            if has_pyside(p):
                remember(p)
                return p

    return ""


def main() -> int:
    # ⚠️ 先确认界面依赖在不在。
    #
    #    双击 .pyw 用的是系统关联的解释器（可能是 WindowsApps 里那个），
    #    如果它没装 PySide6，窗口还没出来就崩了 ——
    #    而 pythonw 没有控制台，用户只看到"双击了没反应"。
    py = _pick_python()
    if not py:
        _say(
            "找不到装了 PySide6 的 Python。\n\n"
            "界面要它才能画出来。装一下：\n"
            "    python -m pip install PySide6\n\n"
            "装完之后，用**这个** Python 再跑：\n"
            "    <那个python的路径>\\start_desktop.pyw"
        )
        return 1

    # 当前解释器不行的话，换一个重跑自己。
    #
    #    不能就地 import —— 依赖是按进程算的，
    #    这个进程里的 sys.path 已经定死了。
    if os.path.abspath(py) != os.path.abspath(sys.executable):
        os.execv(py, [py, os.path.abspath(__file__)] + sys.argv[1:])
        return 1

    miss = _missing()
    if miss:
        # 有控制台就把话说清楚；没有就弹个框。
        msg = (
            "找不到启动需要的文件：\n\n"
            f"{miss}\n\n"
            "可能是更新时文件没同步过来。"
        )
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(0, msg, "dsh-bridge", 0x10)
        except Exception:  # noqa: BLE001
            print(msg)
        return 1

    # 用一个无窗口的 Python 拉起接口服务，别开黑框
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable

    if not port_ok(API_PORT):
        try:
            subprocess.Popen(
                [pyw, API_PY],
                cwd=HERE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=0x08000000 | 0x00000008,  # NO_WINDOW | DETACHED
            )
            # 告诉界面：后台已经在起了，你只管等它，别再起第二个。
            # 两个进程抢同一个端口，只会有一个 listen 成功，日志还难看。
            os.environ["DSH_BRIDGE_BACKEND_SPAWNED"] = "1"
        except OSError:
            pass

        # ⚠️ 这里原来要死等端口最多 15 秒，等到了才开界面。
        #    结果是"双击之后先空白十几秒，窗口才出来"。
        #    现在不等了 —— 界面自己先出来，后台进度显示在它上面，
        #    用户至少知道程序已经开了。

    # ── 看门狗：桌面端开着它就在，桌面端关了它就停 ──
    #
    # ⚠️ 它挂在这个启动器下面，不挂在界面窗口下面。
    #
    #    为什么：界面是 subprocess.call 起的，界面一退这个函数就往下走；
    #    api_server 是 DETACHED 起的，比界面活得久。
    #    守护必须比"被守护的服务"活得久一点，但又不能比用户长 ——
    #    用户关掉派活台，就是不想让它在后台待着。
    #
    #    所以：进来的时候起，界面退出之后叫它走。
    _dog = _start_watchdog(pyw)

    # 起界面。用同一个解释器，保持依赖一致。
    os.chdir(HERE)
    try:
        return subprocess.call([sys.executable, UI_PY], env=os.environ.copy())
    finally:
        # ⚠️ 一定要在 finally 里。
        #
        #    界面可能是正常关的，也可能是崩的、被强杀的 ——
        #    但不管是哪种，用户都看不见窗口了。
        #    这时候看门狗还留着一个后台进程，就是"我不知道它为什么还活着"。
        _stop_watchdog(_dog)


def _dog_dir() -> str:
    """看门狗的停止标记放在哪 —— 跟 watchdog.py 的 BRIDGE_DIR 一致。"""
    return HERE


def _start_watchdog(pyw: str) -> subprocess.Popen | None:
    """起看门狗。已经有在跑的就不再起第二个。

    ⚠️ 判重换了个轻办法：**看它写的心跳文件**。
       原来想过去枚举进程命令行，但那要起一次 PowerShell
       （WMI 查询很慢），每次开桌面端都卡一下不值当。

       看门狗自己会写 watchdog.alive（见 watchdog.py 的 _touch_alive），
       有它且足够新，就说明已经在盯着了。
    """
    dog_py = os.path.join(HERE, "tools", "watchdog.py")
    if not os.path.exists(dog_py):
        return None

    # 已经在跑？
    alive = os.path.join(HERE, "watchdog.alive")
    try:
        if os.path.exists(alive):
            age = time.time() - os.path.getmtime(alive)
            if age < 30:
                return None
    except OSError:
        pass

    # 起之前把旧的停止标记清掉（上次残留的话会让它一起来就退）
    stop_file = os.path.join(HERE, "watchdog.stop")
    try:
        if os.path.exists(stop_file):
            os.remove(stop_file)
    except OSError:
        pass

    try:
        return subprocess.Popen(
            [pyw, dog_py],
            cwd=HERE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=0x08000000 | 0x00000008,  # NO_WINDOW | DETACHED
        )
    except OSError:
        return None


def _stop_watchdog(dog: subprocess.Popen | None) -> None:
    """叫看门狗退出。

    ⚠️ 先写文件让它自己走，等它；等不到才强杀。

       直接 kill 的话，它可能正在重启服务 —— 砍在中间，
       服务停在半路，restart.result 也不写，
       桌面端下次打开会以为"还在重启中"。
    """
    stop_file = os.path.join(HERE, "watchdog.stop")
    try:
        with open(stop_file, "w", encoding="utf-8") as f:
            f.write("desktop closed\n")
    except OSError:
        pass

    if dog is None:
        return

    try:
        dog.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            dog.kill()
        except OSError:
            pass
    # 标记留着没意义，下次起来会被 _start_watchdog 清掉；
    # 这里顺手删了，免得用户看到个莫名其妙的文件。
    try:
        if os.path.exists(stop_file):
            os.remove(stop_file)
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
