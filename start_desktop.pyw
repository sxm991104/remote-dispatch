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
API_PORT = 8790

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
                capture_output=True, timeout=25,
                creationflags=0x08000000,   # NO_WINDOW
            )
            return r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    # 一、当前这个
    if has_pyside(sys.executable):
        return sys.executable

    # 二、同目录的 python.exe（pythonw 旁边通常有一个）
    d = os.path.dirname(sys.executable)
    for name in ("python.exe", "pythonw.exe"):
        p = os.path.join(d, name)
        if has_pyside(p):
            return p

    # 三、PATH
    for name in ("python", "python3"):
        p = shutil.which(name)
        if has_pyside(p):
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
        except OSError:
            pass

        # 等它能响应，最多 15 秒
        for _ in range(30):
            time.sleep(0.5)
            if port_ok(API_PORT):
                break

    # 起界面。用同一个解释器，保持依赖一致。
    os.chdir(HERE)
    return subprocess.call([sys.executable, UI_PY])


if __name__ == "__main__":
    sys.exit(main())
