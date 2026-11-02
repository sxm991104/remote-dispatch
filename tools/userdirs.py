"""各平台该把配置放哪儿。

⚠️ 为什么需要这个文件

    原来全项目有 18 处这样写：

        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        p = os.path.join(base, "dsh-bridge", "xxx.json")

    在 Windows 上是对的（%APPDATA%\\dsh-bridge\\）。
    但换到 Linux：

        APPDATA 不存在 → 退到 ~ → ~/dsh-bridge/xxx.json

    也就是配置散落在家目录的**根**下面。这不合 Linux 的规矩：
    家目录是用户自己的地方，程序不该往那儿扔文件夹。
    XDG 规范说的是 $XDG_CONFIG_HOME（默认 ~/.config）。

    实测（WSL2 Ubuntu 24.04）：
        token 生成在 /root/dsh-bridge/api_token.txt
        而 status.sh 去 /root/.config/dsh-bridge/ 找 → 找不到

    所以抽成这一个函数，所有地方都走它。

各平台的正确位置
    Windows   %APPDATA%\\dsh-bridge\\
              （Roaming，跟着用户漫游。微软的推荐就是这个）

    macOS     ~/Library/Application Support/dsh-bridge/
              （Apple 的规矩：Application Support 放程序数据）

    Linux     $XDG_CONFIG_HOME/dsh-bridge/  默认 ~/.config/dsh-bridge/
              （XDG Base Directory Specification）

要不要考虑 Local AppData / 缓存目录
    不用。这个程序存的是配置和状态（token、任务表、计数），
    都要跨机器/跨会话保留，不是可丢的缓存。

测试怎么隔离
    tests/conftest.py 通过改 APPDATA / LOCALAPPDATA / XDG_CONFIG_HOME
    让每个测试有自己的目录。所以这里**只读环境变量**，
    不在导入时缓存路径 —— 否则测试改了环境变量也不生效。
"""

from __future__ import annotations

import os
import sys

APP_NAME = "dsh-bridge"


def _home() -> str:
    return os.path.expanduser("~")


def config_dir(app: str = APP_NAME) -> str:
    """配置目录（会建）。按平台走各自的规矩。"""
    platform = sys.platform

    if platform == "win32":
        # ⚠️ 只认 APPDATA，不退回 home ——
        #    Windows 上 APPDATA 一定存在，退回去说明环境坏了，
        #    这时候把配置建在 C:\\Users\\xxx\\dsh-bridge 更糟。
        base = os.environ.get("APPDATA") or os.path.join(_home(), "AppData", "Roaming")
    elif platform == "darwin":
        base = os.path.join(_home(), "Library", "Application Support")
    else:
        # Linux / 其他 POSIX：XDG
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home(), ".config")

    d = os.path.join(base, app)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def data_dir(app: str = APP_NAME) -> str:
    """可写的数据目录（会建）。

    Windows 和 config_dir 是同一个（%APPDATA%）。
    其他地方跟 config_dir 也一样 —— 这个程序没有"大的二进制缓存"，
    不需要分 XDG_DATA_HOME。

    留着这个函数是为了以后要拆时不用改所有调用方。
    """
    return config_dir(app)


def config_file(name: str, app: str = APP_NAME) -> str:
    """配置目录下的某个文件（不建文件，只拼路径）。"""
    return os.path.join(config_dir(app), name)


def local_dir(app: str = APP_NAME) -> str:
    """"本机私有、不漫游"的数据目录（会建）。

    Windows 上是 %LOCALAPPDATA%\\dsh-bridge\\
    其他地方跟 config_dir 相同。

    谁用这个：防篡改计数的**影子副本**。
    它故意和主文件分开存（主在 Roaming，影子在 Local），
    这样改一处不会两处一起变。
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            _home(), "AppData", "Local"
        )
        d = os.path.join(base, app)
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        return d
    return config_dir(app)
