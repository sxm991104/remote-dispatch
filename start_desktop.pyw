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


def main() -> int:
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
