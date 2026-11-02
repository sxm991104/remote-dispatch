"""插件里的小工具：找到 dsh-bridge 的配置目录。

⚠️ 为什么插件不能直接 import tools.userdirs

    插件是被 AstrBot 加载的，AstrBot 启动时：
        一、把插件目录加进 sys.path
        二、但**不会**把我们的仓库根加进去

    所以插件里写 import tools.userdirs 会 ModuleNotFoundError。

    而插件又需要知道配置在哪（要读 api_token.txt 调桥的接口）。

    所以这段逻辑在插件这边单独写一份。

    两边必须一致 —— 这里有个测试会比对
    （tests/test_userdirs_consistency.py），
    改了一边忘了另一边会被抓出来。

约定
    Windows   %APPDATA%\\dsh-bridge\\
    macOS     ~/Library/Application Support/dsh-bridge/
    其他 POSIX $XDG_CONFIG_HOME/dsh-bridge/ 或 ~/.config/dsh-bridge/
"""

from __future__ import annotations

import os
import sys


def config_dir(app: str = "dsh-bridge") -> str:
    """配置目录（不保证存在，调用方自己 makedirs）。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming"
        )
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config"
        )
    return os.path.join(base, app)


def config_file(name: str, app: str = "dsh-bridge", create: bool = False) -> str:
    """配置目录下的某个文件。"""
    d = config_dir(app)
    if create:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
    return os.path.join(d, name)
