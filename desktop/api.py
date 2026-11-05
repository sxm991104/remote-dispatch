"""桌面端跟接口服务之间的那层。

## 为什么单独拆出来

    原来这些在 desktop/app.py 里（4236 行那个文件）。
    它们跟界面没关系 —— 只管"发一个请求，拿到结果，回调出去"。

    拆出来之后：
        · 改请求逻辑只开这一个文件
        · 界面各页只 import 这里，不关心 HTTP 细节
        · 能单独测（不用把整个窗口画出来）

## 依赖

    只用标准库（urllib / json / threading）和 PySide6 的 QObject/Signal。
    **零项目内部依赖** —— 它跟 theme.py 一样是最底层。
    （要靠 token 路径的话，用的是 tools.userdirs，那是独立的小工具。）

## 线程模型

    请求在后台线程发，结果通过 Qt 信号回到主线程。
    ⚠️ 不能在后台线程里直接碰控件 —— 会静默失效（不报错，但界面不变）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

# ⚠️ 把自己所在的仓库根目录塞进 sys.path。
#
#    这个文件住在 desktop/ 里，而它要 import 的 tools.userdirs
#    在仓库根下面。从 desktop/api.py 启动时 sys.path[0] 是 desktop/。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import QObject, Signal

# 各平台该把配置放哪儿（%APPDATA% / ~/.config / ...），见 tools/userdirs.py。
import tools.userdirs as _userdirs

# 端口只在这里写一次 —— 从 app.py 那边传进来会形成反向依赖。
# 两边都读同一个环境变量，值一定一致。
_API_PORT = int(os.environ.get("DSH_BRIDGE_PORT") or 8790)
API = f"http://127.0.0.1:{_API_PORT}"


def _read_api_token() -> str:
    """读桥接口的访问 token。

    桥启动时会生成它（在用户目录下的 dsh-bridge/api_token.txt），
    桌面端每次启动读一次，请求时带上。

    ⚠️ 为什么要 token：
        那个接口能派活、能读写文件、能跑命令。
        只靠"绑了 127.0.0.1"是单薄的一道墙——
        本机上任何一个程序、任何一个网页都能来调。
        加个 token，本机别的程序就算知道端口也调不动。
    """
    import os

    p = _userdirs.config_file("api_token.txt")
    try:
        if os.path.exists(p):
            return open(p, encoding="utf-8").read().strip()
    except OSError:
        pass
    return ""


# 启动时读一次，之后每次请求会重新读。
#
# ⚠️ 为什么不能只在启动时读一次：
#     桌面端和接口服务是两个进程，谁先起来不确定。
#     用户先开桌面端、后开接口服务的时候，桌面端启动时
#     那个文件还不存在，读到空字符串 → 之后所有请求都 401。
#     表现是"界面全空，像坏了"。
#
#     文件很小（44 字节），每次读一下几乎不花时间。
_API_TOKEN_CACHE = {"t": _read_api_token()}



def _token() -> str:
    """拿 token。没有就去读，读到就缓存。"""
    if _API_TOKEN_CACHE["t"]:
        return _API_TOKEN_CACHE["t"]
    t = _read_api_token()
    if t:
        _API_TOKEN_CACHE["t"] = t
    return t


# 兼容旧引用

class _TokenProxy:
    def __str__(self) -> str:
        return _token()

    def __eq__(self, other) -> bool:
        return _token() == other

    def __bool__(self) -> bool:
        return bool(_token())


API_TOKEN = _TokenProxy()



class Api(QObject):
    # request_once 的专用通道。
    #
    # ⚠️ 这里犯过一次重复的错：
    #    request_once 跑在后台线程，拿到数据后想回主线程调 callback，
    #    一开始写的是 QTimer.singleShot(0, lambda: callback(data))。
    #    和之前扫码头那次一模一样——从非主线程调 QTimer，
    #    回调根本不会执行。表现就是设置页的路径框永远空着。
    #
    #    规律：**后台线程要碰界面，只能走 Signal。没有第二种办法。**
    _once = Signal(object)
    """后台请求。

    两种用法：
        get/post      结果走全局 done 信号，给周期性刷新用
        request_once  结果只回调一次，给"重启服务"这种一次性动作用

    ⚠️ 为什么要有 request_once：
        一开始重启服务也连的是全局 done。结果轮询刷新（每 3 秒一次）
        的返回值全灌进了重启那个回调，于是每收到一次数据就弹一个
        "没成"对话框——实测叠了 84 层模态窗，界面直接卡死。

        教训：凡是"只该发生一次"的动作，就不能挂在会反复触发的信号上。
    """

    done = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending_cb = None
        self._once.connect(self._dispatch_once)
        # 同一个路径的请求还没回来时不再发第二个。
        #
        # 轮询是每 3 秒一轮，而一次请求最长等 6 秒。后台忙的时候
        # 会有两三个同路径的请求叠着，回来还是乱序的 ——
        # 界面先看到新的、再被旧的盖回去。
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()

    def _dispatch_once(self, payload) -> None:
        """收到请求结果，调它自己的回调。"""
        data, cb = payload if isinstance(payload, tuple) else (payload, None)
        if cb:
            try:
                cb(data)
            except Exception:  # noqa: BLE001
                pass

    def get(self, path: str) -> None:
        with self._inflight_lock:
            if path in self._inflight:
                return
            self._inflight.add(path)
        threading.Thread(target=self._get, args=(path,), daemon=True).start()

    def _get(self, path: str) -> None:
        try:
            req = urllib.request.Request(
                API + path, headers={"X-Token": _token(), "X-Client": "desktop"}
            )
            with urllib.request.urlopen(req, timeout=6) as r:
                self.done.emit(path, json.loads(r.read()))
        except Exception:  # noqa: BLE001
            self.done.emit(path, None)
        finally:
            with self._inflight_lock:
                self._inflight.discard(path)

    def request_once(self, path: str, body: dict | None = None,
                     timeout: int = 20, callback=None) -> None:
        """只回调一次。不碰全局 done 信号。"""

        def run():
            try:
                if body is None:
                    req = urllib.request.Request(
                        API + path, headers={"X-Token": _token(), "X-Client": "desktop"}
                    )
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        data = json.loads(r.read())
                else:
                    req = urllib.request.Request(
                        API + path,
                        data=json.dumps(body).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "X-Token": _token(),
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    data = json.loads(e.read())
                except Exception:  # noqa: BLE001
                    data = {"ok": False, "error": f"HTTP {e.code}"}
            except Exception as e:  # noqa: BLE001
                data = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if callback:
                # ⚠️ 回调必须跟着数据一起传，不能存到一个共享变量里。
                #
                #    原来写的是 self._pending_cb = callback，然后 emit。
                #    两个请求同时发出时，后一个会把前一个的回调覆盖掉，
                #    于是前一个的数据回来时调的是别人的回调——
                #    表现是"这次打开设置页什么都没有，下次又有了"。
                #    这种间歇性的 bug 最难查，因为它是竞态。
                #
                #    改成把回调放进信号参数里发出去，每个请求各回各的。
                self._once.emit((data, callback))

        threading.Thread(target=run, daemon=True).start()

    def post(self, path: str, body: dict, timeout: int = 20) -> None:
        threading.Thread(
            target=self._post, args=(path, body, timeout), daemon=True
        ).start()

    def _post(self, path: str, body: dict, timeout: int = 20) -> None:
        try:
            req = urllib.request.Request(
                API + path,
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Token": _token(),
                    # ⚠️ 告诉服务端"我是本机桌面端"。
                    #    有些接口（比如 /api/guard）会把明文邮箱、
                    #    SMTP 账号只发给桌面端，前台拿不到。
                    "X-Client": "desktop",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                self.done.emit(path, json.loads(r.read()))
        except urllib.error.HTTPError as e:
            try:
                self.done.emit(path, json.loads(e.read()))
            except Exception:  # noqa: BLE001
                self.done.emit(path, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.done.emit(path, {"error": f"{type(e).__name__}: {e}"})


# ────────────────────────────────────────────────────────────────────
# 自绘组件
# ────────────────────────────────────────────────────────────────────

