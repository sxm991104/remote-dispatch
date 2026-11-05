"""桥的 HTTP 接口，给桌面面板用。

为什么要有这一层
    面板如果每次都去起一个 python 进程调 bridge.py，会有两个问题：
        1. 慢——每次几百毫秒，界面上点点点就卡
        2. 吵——一堆进程来回起，日志也乱

    所以桥开一个常驻的本地 HTTP 服务，面板直接请求它。
    只监听 127.0.0.1，不对外。

设计上守住的一条
    **面板只认这层接口，不认 AstrBot。**
    以后 AstrBot 换成自研的，面板不用改；后台从 DeepSeek 换成别的，面板也不用改。
    桥是那个稳定的中间层——今天把任务端从 codebuddy 换到官方 API，
    桥一行没动，就是这个道理。

接口
    GET  /api/tasks              任务列表（带状态、花费、时长）
    GET  /api/task/<名字>        单个任务详情
    GET  /api/task/<名字>/log    这个任务的历史轮次
    POST /api/dispatch           派活   {task, name}
    POST /api/continue           续话   {name, message}
    POST /api/cancel             停     {name}
    GET  /api/live               正在跑的任务 + 当前动作
    GET  /api/outbox             未读的完成通知
    GET  /api/health             存活检查
    GET  /                       面板页面（浏览器打开就是界面）

    前端轮询 /api/tasks 就行，几百毫秒一次都不重——读的是 state 目录里的小文件。

启动
    python api_server.py            # 默认 8790
    python api_server.py --port 9000
"""

from __future__ import annotations

# ⚠️ Windows CI 上 stdout 的默认编码是 cp1252（英文系统），
#    里面没有 ✅ ✗ 这些符号，print 会直接抛 UnicodeEncodeError。
#    实测：整个测试一条都没跑就崩了，而 Ubuntu 上一直是绿的。
#
#    下面这两行必须在任何 print 之前执行。
try:
    from tools.console import mark_utf8 as _mark_utf8
except ImportError:
    import os as _os0, sys as _sys0
    _sys0.path.insert(0, _os0.path.dirname(_os0.path.dirname(_os0.path.abspath(__file__))))
    from tools.console import mark_utf8 as _mark_utf8
_mark_utf8()

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs

def _app_dir() -> str:
    """程序所在目录。源码运行时是脚本目录，打包成 exe 后是 exe 所在目录。

    ⚠️ 打包之后 sys._MEIPASS 是 PyInstaller 每次启动解压出来的临时目录，
        每次都不一样。数据文件（tasks.jsonl、state/、workspaces/）如果
        跟着它走，用户下次启动就看不到上次的任务了。
        所以：
            只读资源（html）  → 从 _MEIPASS 读
            运行数据          → 固定在 exe 旁边
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # ⚠️ 这个文件现在住在 server/ 里。
    #
    #    运行数据（tasks.jsonl、state/、workspaces/）一直在仓库根目录。
    #    如果按 __file__ 算，就变成 server/tasks.jsonl——
    #    等于升级一次把历史任务全"丢了"。
    #
    #    所以往上退一层。
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _res_dir() -> str:
    """只读资源目录（html 等）。打包后在 _MEIPASS 里。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    # html 跟这个脚本放在一起（server/ 里）
    return os.path.dirname(os.path.abspath(__file__))


BRIDGE_DIR = _app_dir()
# 让 `from core import bridge` 找得到——脚本模式下 sys.path[0] 是 server/
sys.path.insert(0, BRIDGE_DIR)

from core import bridge  # noqa: E402

# 桌面端要能自己起停服务，不经过任何命令行窗口。
# launcher 模块就是干这个的，它把 bat 里的活重做成了 Python。
sys.path.insert(0, os.path.join(BRIDGE_DIR, "desktop"))
try:
    from launcher import Launcher, find_qq, port_open, QQ_PORT, ASTRBOT_PORT  # noqa: E402

    _launcher = Launcher()
except Exception:  # noqa: BLE001
    _launcher = None

DEFAULT_PORT = 8790


def _auditor_key_for(base: str) -> str:
    """这个地址是不是审计员配的那个？是就返回它的 Key。

    ## ⚠️ 为什么需要这个

        审计员的 Key 存起来之后，界面上**不回填**（防复制），
        只留一句"已保存 · 尾号 xxx"。

        于是用户点「测试连接」时，界面上传过来的 body 里
        根本没有 api_key —— 只知道地址和模型名。

        这时候如果直接去 _profile_creds() 找，
        拿到的是**任务端模型**的 Key。
        拿 DeepSeek 的 Key 去连硅基流动，必然 401。

        （实测 2026-11-05：老爸说"填写测试是通了，
          但是保存后再测试，返回 401"——就是这个。）

    ## 怎么认

        比 base_url 的**主机名**（忽略路径和末尾斜杠）。
            填 "https://api.siliconflow.cn"  存 "https://api.siliconflow.cn/v1"
            → 是同一个服务，用审计员的 Key

        主机不一样就不碰 —— 用户可能是在测任务端那套模型。
    """
    from urllib.parse import urlparse

    def host(u: str) -> str:
        try:
            h = urlparse(str(u or "").strip()).netloc.lower()
            return h.split(":")[0]
        except Exception:  # noqa: BLE001
            return ""

    want = host(base)
    if not want:
        return ""

    try:
        from guard import auditor as _aud

        cfg = _aud.load() or {}
        if host(cfg.get("base_url") or "") == want:
            return str(cfg.get("api_key") or "").strip()
    except Exception:  # noqa: BLE001
        pass

    return ""


def _profile_creds(pid: str, base: str = "") -> tuple[str, str]:
    """从多套配置里取一套的 (base_url, api_key)。

    优先级：
        传了 id → 就找那套
        没传   → active 那套
        还没有 → 第一套启用的
        都没有 → 退回旧的单套配置（兼容老数据）

    为什么要有这个函数：
        /api/engine/models 和 /api/engine/test 原来都直接读
        engine_bridge.load_config()——那是旧单套配置。
        现在配置是多套的，得按 id 找。
    """
    try:
        from engine import profiles as _pf

        data = _pf.load_all()
        profs = data.get("profiles") or []

        target = None
        if pid:
            target = next((x for x in profs if x.get("id") == pid), None)
        if target is None:
            act = data.get("active") or ""
            target = next((x for x in profs if x.get("id") == act), None)
        if target is None:
            target = next((x for x in profs if x.get("enabled", True)), None)
        if target is None and profs:
            target = profs[0]
        if target:
            return (
                (base or target.get("base_url") or "").strip(),
                (target.get("api_key") or "").strip(),
            )
    except Exception:  # noqa: BLE001
        pass

    # 兜底：旧的单套配置
    try:
        from core import engine_bridge

        cur = engine_bridge.load_config()
        return (
            (base or cur.get("base_url") or "").strip(),
            (cur.get("api_key") or "").strip(),
        )
    except Exception:  # noqa: BLE001
        return (base, "")


def _json(obj, status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps(obj, ensure_ascii=False).encode("utf-8")


# ── 服务状态 ──
#
# 桌面端要给普通人看"现在能不能用"，所以这里只回答三个是/否：
#   qq     QQ 登录了没（NapCat 的 3000 端口通 = 登录着）
#   bot    对话服务在不在（AstrBot 的 6185）
#   bridge 后台任务端在不在（能用 dsh 跑东西）
#
# 不返回任何配置细节、密码、key——这是给人看的面板，不是运维台。

_NAPCAT_PORT = 3000
_BOT_PORT = 6185


def _port_ok(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _qrcode_patterns() -> list[str]:
    """二维码在哪。跟着用户配的 NapCat 目录走，不写死。"""
    import paths

    nap = paths.get("napcat_dir")
    pats = []
    if nap:
        pats.append(os.path.join(nap, "cache", "qrcode.png"))
    # 兜底：常见位置下再找一层
    #
    # ⚠️ 不写死任何盘符。上面 paths.get("napcat_dir") 才是用户配的那个，
    #    这里只是碰运气扫一下几个通用的地方。
    _roots = []
    _explicit = os.environ.get("QQAIBOT_HOME", "").strip()
    if _explicit:
        _roots.append(os.path.join(_explicit, "napcat"))
    _roots += [
        os.path.expandvars(r"%USERPROFILE%\qqaibot\napcat"),
        os.path.expandvars(r"%LOCALAPPDATA%\qqaibot\napcat"),
        os.path.expandvars(r"%APPDATA%\qqaibot\napcat"),
    ]
    for root in _roots:
        pats.append(os.path.join(root, "*", "cache", "qrcode.png"))
        pats.append(os.path.join(root, "*", "*", "cache", "qrcode.png"))
    return pats


def _listening_pids(port: int) -> list[str]:
    """找到监听某个端口的进程（Windows）。"""
    import subprocess

    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, timeout=15,
        ).stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    pids = []
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            pids.append(line.split()[-1])
    return list(dict.fromkeys(pids))


def https_status() -> dict:
    """服务状态，给界面显示。

    ## 每一项都**真去查**，不许写死

        原来这里是：

            return {"qq": False, "feishu": False, "bridge": True}

        三个值全是常量。于是：
            · 顶栏「飞书」永远是红的 —— 哪怕 atrium 正跑着
            · 「任务端」永远是绿的 —— 哪怕这个服务眼看要挂
            · 用户在右上角看到的状态，跟实际情况一点关系都没有

        （2026-11-05 老爸截图里问「为啥只有飞书一直红」，才发现。）

    ## 现在怎么查

        飞书 / 钉钉   看 atrium 的心跳文件（~/.atrium/alive.json）
                      它每 10 秒写一次，超过 60 秒没写就当没跑

        任务端        这里能跑进来就说明它在（否则你看到的就是错误页）

    ## ⚠️ 为什么还留着 "qq" 这个键

        界面按这几个键取值的，删掉会让界面拿到 None。
        QQ 现在不支持了，恒为 False，但键留着。
    """
    import json as _json
    import os as _os
    import time as _t

    out = {"qq": False, "feishu": False, "dingtalk": False, "bridge": True}

    # ── atrium 心跳 ──
    #
    # ⚠️ 心跳超过 60 秒没更新就当没跑。
    #    atrium 那边每 10 秒写一次，60 秒是 6 个周期，够宽容了。
    p = _os.path.join(_os.path.expanduser("~"), ".atrium", "alive.json")
    try:
        with open(p, encoding="utf-8") as f:
            d = _json.load(f)
        age = _t.time() - float(d.get("ts") or 0)
        alive = age < 60
    except (OSError, _json.JSONDecodeError, TypeError, ValueError):
        alive = False

    if alive:
        # atrium 活着，再看它配了哪几个平台
        #
        # 只看"配了没"，不看"连上没" ——
        # 连上没要问 atrium 自己，跨进程问不到，而且心跳活着
        # 本身就是"它在跑"的证据。
        try:
            cfgp = _os.path.join(_os.path.expanduser("~"), ".atrium", "config.json")
            with open(cfgp, encoding="utf-8") as f:
                ac = _json.load(f)
            plats = ac.get("platforms") or {}
            if isinstance(plats, dict):
                fs = plats.get("feishu") or {}
                dt = plats.get("dingtalk") or {}
                out["feishu"] = bool(
                    isinstance(fs, dict) and fs.get("app_id")
                    and fs.get("enabled") is not False
                )
                out["dingtalk"] = bool(
                    isinstance(dt, dict) and dt.get("client_id")
                    and dt.get("enabled") is not False
                )
        except (OSError, _json.JSONDecodeError):
            pass

    return out


def _run_diagnose() -> dict:
    """跑一轮自检，返回结构化报告。前端只管画。"""
    checks: list[dict] = []

    # 1. 引擎服务（这个 HTTP 服务本身）
    checks.append({
        "id": "engine.self",
        "label": "桥服务",
        "ok": True,
        "detail": "桥服务活着，能收到请求",
        "fix": "",
    })

    # 2. 任务端模型配置
    try:
        from core import engine_bridge
        cfg = engine_bridge.load_config()
        has_key = bool(cfg.get("api_key"))
        model = cfg.get("model") or ""
        base = cfg.get("base_url") or ""
        if has_key and model and base:
            checks.append({
                "id": "engine.model",
                "label": "任务端模型",
                "ok": True,
                "detail": f"{model} @ {base}",
                "fix": "",
            })
        else:
            missing = []
            if not base: missing.append("接口地址")
            if not model: missing.append("模型名")
            if not has_key: missing.append("API Key")
            checks.append({
                "id": "engine.model",
                "label": "任务端模型",
                "ok": False,
                "detail": "缺：" + "、".join(missing),
                "fix": "去「模型」页填接口地址、模型名和 Key",
            })
    except Exception as e:  # noqa: BLE001
        checks.append({
            "id": "engine.model",
            "label": "任务端模型",
            "ok": False,
            "detail": f"读配置失败：{e}",
            "fix": "去「模型」页重新配置",
        })

    # 3. atrium 配置
    #
    # ⚠️ 路径在**用户主目录**下：~/.atrium/config.json
    #
    #    原来拼的是 %APPDATA%\.atrium\config.json ——
    #    那是 C:\Users\xxx\AppData\Roaming\.atrium\，
    #    而 atrium 实际写在 C:\Users\xxx\.atrium\。
    #
    #    差了一层目录，永远读不到，于是诊断永远报
    #    "飞书和钉钉都没填" —— 用户明明配好了。
    #
    #    实测（2026-11-05）：真开窗口点「诊断」看到的。
    #
    #    现在两个位置都找一遍（老版本可能真的装到过 APPDATA 下），
    #    找到哪个用哪个。
    import json as _json_mod

    _atrium_cands = [
        # 现在的位置：用户主目录
        os.path.join(os.path.expanduser("~"), ".atrium", "config.json"),
        # 兜底：万一有人的装到了 APPDATA 下
        os.path.join(os.environ.get("APPDATA", ""), ".atrium", "config.json"),
    ]
    atrium_cfg = ""
    for _c in _atrium_cands:
        if _c and os.path.exists(_c):
            atrium_cfg = _c
            break
    if not atrium_cfg:
        atrium_cfg = _atrium_cands[0]

    try:
        with open(atrium_cfg, encoding="utf-8") as f:
            acfg = _json_mod.load(f)

        # ⚠️ 平台配置在 acfg["platforms"]["feishu"]，不是 acfg["feishu"]。
        #
        #    atrium 的 config.json 结构是：
        #        engine / storage / platforms{feishu{...}, dingtalk{...}}
        #
        #    原来直接读 acfg["feishu"]，那层压根不存在，
        #    所以就算路径对了也会判成"没填"。
        _plats = acfg.get("platforms") or {}
        for _k, _v in _plats.items() if isinstance(_plats, dict) else []:
            if not isinstance(_v, dict):
                continue
            if _v.get("enabled") is False:
                continue
        feishu = bool(
            ((_plats.get("feishu") or {}).get("app_id")
             if isinstance(_plats, dict) else False)
        )
        dingtalk = bool(
            ((_plats.get("dingtalk") or {}).get("client_id")
             if isinstance(_plats, dict) else False)
        )
        if feishu or dingtalk:
            chans = []
            if feishu:
                chans.append("飞书")
            if dingtalk:
                chans.append("钉钉")
            checks.append({
                "id": "atrium.config",
                "label": "atrium 通道",
                "ok": True,
                "detail": "已配：" + "、".join(chans),
                "fix": "",
            })
        else:
            checks.append({
                "id": "atrium.config",
                "label": "atrium 通道",
                "ok": False,
                "detail": f"配置里没有可用的平台（{atrium_cfg}）",
                "fix": "去「通道」页填飞书或钉钉凭据",
            })
    except FileNotFoundError:
        checks.append({
            "id": "atrium.config",
            "label": "atrium 通道",
            "ok": False,
            "detail": f"找不到配置 {atrium_cfg}",
            "fix": "在 atrium 目录跑 python -m atrium.cli init 生成配置",
        })
    except Exception as e:  # noqa: BLE001
        checks.append({
            "id": "atrium.config",
            "label": "atrium 通道",
            "ok": False,
            "detail": f"读配置失败：{e}",
            "fix": "去「通道」页检查配置",
        })


    # 4. 任务锁（有没有卡死的任务）
    try:
        state_dir = os.path.join(BRIDGE_DIR, "state")
        locks = []
        if os.path.isdir(state_dir):
            for name in os.listdir(state_dir):
                if name.endswith(".lock"):
                    locks.append(name)
        if not locks:
            checks.append({
                "id": "engine.locks",
                "label": "卡死任务锁",
                "ok": True,
                "detail": "没有残留锁",
                "fix": "",
            })
        else:
            checks.append({
                "id": "engine.locks",
                "label": "卡死任务锁",
                "ok": False,
                "detail": f"{len(locks)} 个锁文件残留：{', '.join(locks[:3])}",
                "fix": "重启桥服务会清掉；或手动删 state/ 下的 .lock",
            })
    except Exception as e:  # noqa: BLE001
        checks.append({
            "id": "engine.locks",
            "label": "卡死任务锁",
            "ok": True,
            "detail": f"检查跳过：{e}",
            "fix": "",
        })

    return {"ok": True, "checks": checks}


# 本机接口的访问 token。main() 启动时生成/读取，_guard() 用它校验。
_API_TOKEN = ""


class Handler(BaseHTTPRequestHandler):
    server_version = "dsh-bridge-api/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        # 面板轮询很频繁，别把每条都写进控制台
        pass

    # ── 工具 ──

    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # ⚠️ 这里原来是 Access-Control-Allow-Origin: "*"。
        #
        #    通配符的意思是"任何网站都可以跨源读我的响应"。
        #    这个接口能派活、能读任务内容，不该让随便哪个网页读。
        #
        #    改成只回显本机来源。面板页面由同一个服务发出（同源），
        #    本来就不需要 CORS；留一个本机来源是给调试用的。
        origin = self.headers.get("Origin", "")
        if origin.startswith(("http://127.0.0.1", "http://localhost")):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, b"")

    def _guard(self) -> bool:
        """三层防护，过不了就直接回绝。

        第一层  地址必须是本机（绑了 127.0.0.1，这里是第二道确认）
        第二层  带浏览器 Origin 头的拒绝（防恶意网页偷调）
        第三层  token 必须对

        为什么 token 放最后检查：
            前面两层是便宜的判断，token 要读文件、比字符串。
            先便宜的、后贵的。

        例外：/api/health 不需要 token。
            它是给"服务活没活着"用的，不碰任何数据，
            桌面端和启动器都要在拿 token 之前问它。
        """
        from guard import security as _sec

        # 第一层：只接受本机连接
        if not _sec.is_local_address(self.client_address[0]):
            self._send(403, b'{"error":"only local connections"}')
            return False

        # DNS rebinding 检查：Host 头必须是本机名。
        #
        # 攻击者把 evil.com 的 DNS 指到 127.0.0.1，
        # 用户打开 evil.com 时，请求会打到我们这里，
        # 但 Host 头是 evil.com。这种请求直接拒。
        if not _sec.host_is_local(self.headers):
            self._send(403, b'{"error":"bad host"}')
            return False

        # 第二层：浏览器一律拒绝。
        #
        # 这条挡的是：用户打开了一个恶意网页，那个网页
        # 往 localhost:8790 POST 一个任务。浏览器能发出去
        # （跨源只拦读响应，不拦发请求），而派活不需要读响应。
        if _sec.has_browser_origin(self.headers):
            self._send(403, b'{"error":"browser requests not allowed"}')
            return False

        # 第三层：token
        if getattr(self, "path", "") in ("/api/health",):
            return True

        # 恒定时间比较 + 空 token 一律拒。
        #
        # ⚠️ 原来写的是 `if want and got != want`——
        #    want 为空时整句短路成 False，等于**放行所有人**。
        #    token 生成失败（文件写不进去）的时候就是这个状态。
        #    现在 token_matches 明确处理了空的情况。
        got = self.headers.get("X-Token", "")
        if not _sec.token_matches(got, _API_TOKEN):
            self._send(401, b'{"error":"bad or missing X-Token"}')
            return False

        return True

    # 请求体最大多少。
    #
    # 实测过：不限制的话，一个 2MB 的 task 字段会被原样接受，
    # 然后喂给模型——那个任务的 token 费用能烧掉一大笔钱。
    # 而且超大请求体本身也是种资源消耗。
    #
    # 1MB 足够用了：最长的任务描述也就几万字。
    MAX_BODY = 1024 * 1024

    @staticmethod
    def _str_field(body: dict, key: str, default: str = "") -> str:
        """从请求体里取一个字符串字段。

        ⚠️ 为什么不能直接用 body.get(key)：
            攻击者传 {"task": 123} 时，拿到的是 int，
            后面调 .strip() 就崩，连接断掉。
            {"name": [1,2]} 同理。
            这类崩溃不算能利用，但是拒绝服务——请求不是干净地
            回 400，而是把连接弄断。

        非字符串一律当没给（走下面的"必填"检查回 400）。
        """
        v = body.get(key)
        if not isinstance(v, str):
            return default
        return v

    def _body(self) -> dict:
        """读请求体。

        ⚠️ 必须校验解析出来的类型。
            原来直接 json.loads 就往回返，于是：
                body = null   → None，后面 body.get() 崩
                body = []     → list，同样崩
                body = 123    → int，崩
            这些请求会让连接直接断掉（不是干净地回错误码）。
            攻击者拿这个就能做拒绝服务。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0

        if n <= 0:
            return {}

        if n > self.MAX_BODY:
            # 不读、直接拒。读了就白消耗内存。
            self._send(413, b'{"error":"request body too large"}')
            return None

        try:
            raw = self.rfile.read(n)
        except OSError:
            return {}

        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, b'{"error":"bad json"}')
            return None

        # 必须是对象（dict）。数组、字符串、数字、null 一律不当请求体。
        if not isinstance(data, dict):
            self._send(400, b'{"error":"body must be a json object"}')
            return None

        return data

    # ── 路由 ──

    def do_GET(self):  # noqa: N802
        if not self._guard():
            return
        path = unquote(urlparse(self.path).path)

        # 根路径直接给面板页面。
        # 为什么由这个服务来发：双击 html 打开的话是 file:// 协议，
        # fetch 到 127.0.0.1 会被浏览器当成跨源请求拦掉。
        # 同一个服务发页面 + 接口，就没有这个问题，用户也不用装任何东西。
        if path in ("/", "/index.html"):
            # 根路径给桌面版界面（desktop_ui.html）；
            # /panel 给早先那个简易版，留着方便对比调试
            for fn in ("desktop_ui.html", "panel.html"):
                f = os.path.join(BRIDGE_DIR, fn)
                if os.path.exists(f):
                    try:
                        return self._send(200, open(f, "rb").read(),
                                          "text/html; charset=utf-8")
                    except OSError:
                        continue
            return self._send(404, b'<h1>UI not found</h1>', "text/html; charset=utf-8")

        if path == "/panel":
            f = os.path.join(_res_dir(), "panel.html")
            if os.path.exists(f):
                return self._send(200, open(f, "rb").read(), "text/html; charset=utf-8")
            return self._send(404, b'<h1>not found</h1>', "text/html; charset=utf-8")

        if path == "/api/health":
            st, b = _json({"ok": True, "time": datetime.now().isoformat(timespec="seconds")})
            return self._send(st, b)

        if path == "/api/diagnose":
            return self._send(*_json(_run_diagnose()))

        if path == "/api/tasks":
            return self._tasks()

        if path == "/api/asks":
            # 电脑端卡住时推回来的问题。
            #
            # ⚠️ 为什么不复用 /api/task/<id>：
            #
            #    那个查的是"某一个任务现在怎么样"，
            #    这个是"有哪些任务在等用户回答" —— 跨任务的。
            #
            #    前台要回答「它怎么不动了」只能看这个，
            #    单个任务的状态回答不了。
            #
            # ⚠️ 这个是 GET（跟 /api/tasks 一样）。
            #    limit 从 query string 读，不从 body ——
            #    GET 没有 body，原来那样写会直接崩。
            q = parse_qs(urlparse(self.path).query)
            try:
                limit = int((q.get("limit") or ["5"])[0])
            except (TypeError, ValueError):
                limit = 5
            try:
                st, b = _json({"ok": True, "asks": bridge.read_asks(limit)})
            except Exception as e:  # noqa: BLE001
                st, b = _json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path.startswith("/api/windows"):
            return self._windows(path)

        if path == "/api/live":
            try:
                st, b = _json(bridge.cmd_live())
            except Exception as e:  # noqa: BLE001
                st, b = _json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/channels":
            try:
                from desktop import channels as _ch

                d = _ch.status()
                st, b = _json(d)
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"})
            return self._send(st, b)

        if path == "/api/services":
            try:
                return self._send(*_json(https_status()))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"error": str(e)}, 500))

        if path == "/api/model":
            return self._model_get()

        if path == "/api/paths":
            return self._paths_get()

        if path == "/api/engine":
            return self._engine_get()

        if path == "/api/engine/profiles":
            return self._profiles_get()

        if path == "/api/auditor":
            # 安全审核员。跟主模型分开的两套配置。
            #
            # ⚠️ **api_key 不能原样吐出去。**
            #
            #    原来直接 _json(cfg)，把明文 key 回给了调用方。
            #    界面那边就算显示成圆圈，Ctrl+A / Ctrl+C 也能复制出完整 key。
            #    （实测 2026-11-05：老爸当场复现，"这是很严重的明文 bug"。）
            #
            #    现在跟 /api/engine/profiles 一个做法：
            #        api_key  换成 has_key（有没有）
            #        key_tail 只回尾号四位
            #    前端拿尾号显示"已保存 · 尾号 xxxx"，永远拿不到完整值。
            try:
                from guard import auditor as _aud

                cfg = _aud.load()
                ok, why = _aud.is_ready(cfg)
                cfg["_ready"] = ok
                cfg["_why"] = why

                _k = str(cfg.get("api_key") or "")
                cfg["has_key"] = bool(_k)
                cfg["key_tail"] = _k[-4:] if len(_k) > 4 else ""
                cfg.pop("api_key", None)

                return self._send(*_json(cfg))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"error": str(e)}, 500))


        if path == "/api/perms":
            try:
                return self._send(*_json(self._perms_get()))
            except Exception as e:  # noqa: BLE001
                return self._send(
                    *_json({"ok": False, "error": str(e)}, 500)
                )

        if path == "/api/guard":
            # 验证码邮箱的配置。
            #
            # ⚠️ **明文地址只给桌面端，不给前台。**
            #
            #    原来不分青红皂白返回 email / smtp_user 完整值，
            #    而调用这个接口的还有前台 Agent（模型）。
            #
            #    前台是"收码的柜员"，不是"管邮箱的人"：
            #        它只该知道「要码了」「你配了吗」，
            #        不该知道邮箱是哪个、服务器是哪台。
            #
            #    一旦它知道了，那些字就会回到对话里 ——
            #    被截图、被翻记录，等于把撞密码的目标送出去。
            #
            #    所以：
            #        带 X-Client: desktop 头 → 给全（本机设置页要填要改）
            #        否则（前台 / 任何别的调用方）→ 只给"配好没"
            try:
                from guard import guard

                c = guard.load_config()
                ready = guard.is_guard_ready()

                # 只有本机桌面端才给明细
                who = (self.headers.get("X-Client") or "").strip().lower()
                if who != "desktop":
                    return self._send(*_json({
                        "ok": True,
                        "enabled": bool(c.get("enabled")),
                        "ready": ready,
                        # 够前台判断"能不能发码"了，多的一个字不给
                        "has_email": bool(c.get("email")),
                    }))

                return self._send(*_json({
                    "ok": True,
                    "enabled": c.get("enabled"),
                    "email": c.get("email") or "",
                    "email_masked": guard._mask_email(c.get("email") or ""),
                    "smtp_host": c.get("smtp_host") or "",
                    "smtp_port": c.get("smtp_port") or 465,
                    "smtp_user": c.get("smtp_user") or "",
                    "has_pass": bool(c.get("smtp_pass")),
                    "ready": ready,
                }))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/autostart":
            try:
                import paths

                return self._send(*_json({
                    "ok": True,
                    "enabled": paths.is_autostart(),
                }))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/qrcode":
            return self._qrcode()

        if path == "/api/outbox":
            return self._outbox()

        if path.startswith("/api/task/"):
            rest = path[len("/api/task/"):]
            if rest.endswith("/log"):
                return self._task_log(rest[: -len("/log")])
            return self._task(rest)


        if path == "/api/security/mode":
            # 安全模式。只读。
            #
            # ⚠️ 改模式在 do_POST 里，这里只读 ——
            #    GET 没有 body，走不了 set_mode。
            #
            # ## ⚠️ 但这里会**自动修正**一次
            #
            #     老爸的原话（2026-11-05）：
            #         「你要检测用户有没有回退嘛，有的话就给他打到有的。」
            #
            #     场景：用户本来开着「严格防护」，后来把邮箱关了。
            #         严格防护每拦一次都要发码，码发不出去 → 人卡死。
            #
            #     原来只是把按钮灰掉 —— 等于把问题丢给用户，
            #     而他可能根本不知道为什么。
            #
            #     现在：读状态的时候顺手判一下，
            #         当前模式用不了就**自动换到一个还能用的**，
            #         并在返回里带一句 why_changed 说明发生了什么。
            #
            #     ⚠️ 只降不升：换到的是"能用的里面保护最强的那个"，
            #        不会因为某次探测抖一下就把用户升到严格防护去
            #        （那会突然开始疯狂要码，更烦人）。
            from guard import security_mode as _sm

            st = _sm.status()
            try:
                from guard import capabilities as _cap

                caps = _cap.capabilities()
                cur = str(st.get("mode") or "")
                modes = caps.get("modes") or {}

                if cur and not (modes.get(cur) or {}).get("ok"):
                    pick = _cap.pick_usable(cur, caps)
                    if pick and pick != cur:
                        _sm.set_mode(pick)
                        st = _sm.status()
                        st["changed_from"] = cur
                        st["why_changed"] = _cap.explain_fallback(cur, pick, caps)
                    elif not pick:
                        # 一个都开不了 —— 不强行改，但告诉用户
                        st["no_usable_mode"] = True
                        st["why_changed"] = (
                            "现在一个安全模式都开不了：验证码邮箱和系统卷影副本都不具备。"
                            "去配一样就能选了。"
                        )

                st["capabilities"] = caps
            except Exception as e:  # noqa: BLE001
                # 查能力失败不能把"看状态"这个接口搞挂
                st["capabilities_error"] = f"{type(e).__name__}: {e}"

            return self._send(*_json(st))

        if path == "/api/net/status":
                # 看现在有没有联网授权、还剩多久。只读。
                from guard import netpolicy as _np

                return self._send(*_json(_np.status()))
        return self._send(404, b'{"error":"not found"}')

    def do_POST(self):  # noqa: N802
        """所有 POST 入口。

        外面包一层兜底：任何没预料到的异常，都回一个 500，
        而不是让连接直接断掉。

        为什么要这层：
            实测过 {"task": 123} 这种类型不对的请求能崩到连接断开。
            那算是拒绝服务——客户端拿不到任何错误码，
            只能看到"连接被重置"，排查时一脸茫然。

            内部处理函数各自还有 try，这层是最后一道。
        """
        try:
            self._do_post()
        except Exception as e:  # noqa: BLE001
            try:
                self._send(
                    500,
                    json.dumps(
                        {"error": f"{type(e).__name__}"}, ensure_ascii=False
                    ).encode("utf-8"),
                )
            except Exception:  # noqa: BLE001
                pass

    def _do_post(self):  # noqa: N802
        if not self._guard():
            return
        path = urlparse(self.path).path
        body = self._body()
        if body is None:
            # _body 已经回过错误了（413 太大 / 400 不是对象），别再往下走
            return

        if path == "/api/dispatch":
            task = self._str_field(body, "task").strip()
            name = self._str_field(body, "name").strip()
            profile = self._str_field(body, "profile").strip()
            if not task:
                return self._send(400, b'{"error":"task is required"}')
            if not name:
                return self._send(400, b'{"error":"name is required"}')
            # 名字先过一遍校验，不合法就直接拒，别让它在后面炸
            try:
                from guard.safe_path import safe_name as _sn

                _sn(name)
            except ValueError as e:
                return self._send(
                    400,
                    json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                )
            try:
                st, b = _json(self._dispatch_with_concurrency(task, name, profile))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/continue":
            name = self._str_field(body, "name").strip()
            msg = self._str_field(body, "message", "继续").strip()
            if not name:
                return self._send(400, b'{"error":"name is required"}')
            try:
                rec = bridge.cmd_dispatch(msg, name)
                # ⚠️ 被拦下的不要起 worker（原来无条件 spawn）。

                if rec.get("status") != "blocked":
                    self._spawn(name)
                st, b = _json(rec)
            except Exception as e:  # noqa: BLE001
                st, b = _json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/engine":
            try:
                st, b = _json(self._engine_set(body))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/engine/profiles":
            try:
                st, b = _json(self._profiles_act(body))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/auditor":
            # 存安全审核员配置
            try:
                from guard import auditor as _aud

                cfg = _aud.load()
                for k in ("enabled", "base_url", "api_key", "model",
                          "on_error", "audit_writes", "audit_modifies"):
                    if k in body:
                        cfg[k] = body[k]
                ok = _aud.save(cfg)
                ready, why = _aud.is_ready(_aud.load())
                return self._send(*_json({"ok": ok, "ready": ready, "why": why}))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))


        if path == "/api/auditor/verify":
            # 验审核验证码 → 关掉审核一段时间。
            #
            # ⚠️ 这个接口只接受**码**，不接受"我要关审核"这种命令。
            #
            #    前台会调它（用户回复的码要转过来），
            #    但前台**不知道**码是什么 —— 它只是把用户说的转给这里。
            #    验码在服务端做，前台说了不算。
            from guard import auditor_code as _ac
            from guard import auditor_state as _ast

            code = str(body.get("code") or "").strip()
            dur = str(body.get("duration") or "1h").strip()
            if dur not in _ac.__dict__.get("DURATIONS", {}) and dur not in ("1h", "4h", "today"):
                dur = "1h"

            ok, msg = _ac.verify(code)
            if not ok:
                return self._send(*_json({"ok": False, "error": msg}, 400))

            until = _ast.mark_off(dur, "用户验证通过")
            import time as _t

            return self._send(*_json({
                "ok": True,
                "message": msg,
                "until": until,
                "remaining": max(0, int(until - _t.time())),
            }))


        if path == "/api/net/allow":
            # 验联网验证码 → 放行联网一段时间。
            #
            # ⚠️ 只接受**码**，不接受"我要联网"这种命令。
            #    前台会调它（用户回复的码要转过来），
            #    但前台不知道码是什么 —— 验在服务端。
            from guard import netauth as _na
            from guard import netpolicy as _np

            code = str(body.get("code") or "").strip()
            minutes = int(body.get("minutes") or 30)
            if minutes < 1:
                minutes = 1
            if minutes > 480:
                minutes = 480     # 最长 8 小时

            ok, msg = _na.verify(code)
            if not ok:
                return self._send(*_json({"ok": False, "error": msg}, 400))

            until = _np.grant(minutes, "用户验证码放行联网")
            import time as _t

            return self._send(*_json({
                "ok": True,
                "message": msg,
                "until": until,
                "remaining": max(0, int(until - _t.time())),
            }))

        if path == "/api/net/code":
            # 发联网授权验证码到邮箱。
            #
            # ⚠️ 原来 netauth.send_code() 写好了却没挂路由 ——
            #    桌面端点"申请联网授权"没东西可调，只能干瞪眼。
            #    这里复用它：10 分钟内已发过就复用，不重复打扰。
            from guard import netauth as _na

            ok, msg = _na.send_code()
            return self._send(*_json({"ok": ok, "message": msg}, 200 if ok else 502))

        if path == "/api/net/revoke":
            # 提前收回联网授权。
            #
            # ⚠️ netpolicy.revoke() 一直有函数没挂路由 ——
            #    放行后想提前关掉，界面上没按钮可点。
            from guard import netpolicy as _np

            _np.revoke("用户在桌面端提前收回联网")
            return self._send(*_json({"ok": True, "message": "已收回联网授权"}))



        if path == "/api/auditor/status":
            # 看审核员现在什么状态。只读。
            from guard import auditor as _aud
            from guard import auditor_state as _ast

            s = _ast.get()
            cfg = _aud.load()
            ready, why = _aud.is_ready(cfg)
            return self._send(*_json({
                "state": s["state"],
                "until": s["until"],
                "why": s["why"],
                "enabled": cfg.get("enabled"),
                "ready": ready,
                "ready_why": why,
                "remaining": _ast.remaining(),
            }))




        if path == "/api/security/mode":
            # 改安全模式。
            #
            # ⚠️ 这一段原来是**只读**的 —— 里面只有一句
            #        return self._send(*_json(_sm.status()))
            #
            #    前端点「出事可回退」→ POST 过来 → 服务端只回了一份状态，
            #    **压根没调 set_mode**。所以模式永远停在原来那个。
            #    前端拿到 ok=True 还挺高兴，界面也不报错。
            #
            #    实测（2026-11-05）：老爸点「出事可回退」，
            #    磁盘上 security_mode.json 里还是 "strict"。
            #
            #    注释里原来写的是「安全模式。只读。改模式在 do_POST 里」——
            #    结果这段**就在 do_POST 里**，却是只读的。
            from guard import security_mode as _sm

            mode = self._str_field(body, "mode").strip().lower()
            if not mode:
                # 没说要改哪个，那就是来读状态的
                return self._send(*_json(_sm.status()))

            # ── 能力检查：做不到的模式不许切 ──
            #
            # ⚠️ 桌面端那边已经拦了一道（按钮 disabled + 点之前查），
            #    但那是界面层 —— 任何直接打这个接口的调用方都绕得过。
            #
            #    老爸的原话（2026-11-05）：
            #        「如果都没有设置，就给提示，都开不了，
            #          只有配置了才能开。」
            #
            #    所以服务端必须自己再判一次。三层都拦：
            #        界面禁用 → 点之前查 → **服务端再查**
            try:
                from guard import capabilities as _cap

                caps = _cap.capabilities()
                modes = caps.get("modes") or {}

                # 先看模式本身认不认识 —— 不认识的模式给"不认识"，
                # 不要误报成"现在开不了"（那是两回事，用户会照错的话去配）。
                if mode not in modes:
                    return self._send(*_json({
                        "ok": False,
                        "error": f"不认识的模式：{mode}",
                    }, 400))

                info = modes.get(mode) or {}
                if not info.get("ok"):
                    return self._send(*_json({
                        "ok": False,
                        "error": info.get("why") or "这个模式现在开不了",
                        "capabilities": caps,
                    }, 400))
            except Exception as e:  # noqa: BLE001
                # 查能力失败 —— 不放行。
                # 跟别处"守卫挂了不等于安全"一个方向。
                return self._send(*_json({
                    "ok": False,
                    "error": f"判断不了能不能开这个模式（{type(e).__name__}），先不给你切。",
                }, 500))

            try:
                _sm.set_mode(mode)
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({
                    "ok": False,
                    "error": f"不认识的模式：{mode}（{type(e).__name__}）",
                }, 400))

            st = _sm.status()
            st["ok"] = True
            return self._send(*_json(st))

        if path == "/api/security/mode/verified":
            # 切安全模式 —— **要验证码的那个版本**。
            #
            # ## ⚠️ 为什么要单独一个入口，不直接给 /api/security/mode 加码
            #
            #     桌面端那边是**用户坐在电脑前点**的 ——
            #     他自己看着屏幕，不需要再发个码到自己邮箱。
            #     每次切模式都逼他收码，那是折磨。
            #
            #     而这个入口是**给 atrium 前台用的** ——
            #     消息从飞书/钉钉进来，可能是别人借手机发的。
            #     所以前台一刀切：**切模型就要码。**
            #
            #     （2026-11-05 老爸定的：
            #        「要那个码，到前台了都要码宝贝，
            #          因为前台是暴露在外的，桌面端是用户电脑。」）
            #
            # ## 流程
            #
            #     不带 code  → 发码，回 need_code=True
            #     带 code    → 校验，通过就切
            #
            #     跟 /api/perms 那套是同一个模式（见 2150 行附近），
            #     连 purpose 的拼法都保持一致，省得两套码互相打架。
            from guard import security_mode as _sm2

            mode = self._str_field(body, "mode").strip().lower()
            code = self._str_field(body, "code").strip()

            if not mode:
                st = _sm2.status()
                st["ok"] = True
                return self._send(*_json(st))

            # 能力检查（跟上面那条一样）
            try:
                from guard import capabilities as _cap

                caps = _cap.capabilities()
                modes = caps.get("modes") or {}
                if mode not in modes:
                    return self._send(*_json({
                        "ok": False, "error": f"不认识的模式：{mode}",
                    }, 400))
                info = modes.get(mode) or {}
                if not info.get("ok"):
                    return self._send(*_json({
                        "ok": False,
                        "error": info.get("why") or "这个模式现在开不了",
                        "capabilities": caps,
                    }, 400))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({
                    "ok": False,
                    "error": f"判断不了能不能开这个模式（{type(e).__name__}），先不给你切。",
                }, 500))

            purpose = f"security:{mode}"

            try:
                from guard import guard as _g
            except ImportError:
                return self._send(*_json({
                    "ok": False, "error": "guard 模块不可用，没法验证",
                }, 500))

            if not code:
                ok, msg = _g.send_code_email(purpose, "切安全模式")
                return self._send(*_json({
                    "ok": False,
                    "need_code": True,
                    "mode": mode,
                    "message": msg if ok else f"发码失败：{msg}",
                }))

            ok, msg = _g.verify(purpose, code)
            if not ok:
                return self._send(*_json({
                    "ok": False,
                    "need_code": True,
                    "error": msg,
                }, 400))

            try:
                _sm2.set_mode(mode)
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({
                    "ok": False,
                    "error": f"设不了：{type(e).__name__}: {e}",
                }, 500))

            st = _sm2.status()
            st["ok"] = True
            st["verified"] = True
            return self._send(*_json(st))

        if path == "/api/engine/models":
            try:
                st, b = _json(self._engine_models(body))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/engine/test":
            try:
                st, b = _json(self._engine_test(body))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/perms":
            try:
                return self._send(*_json(self._perms_set(body)))
            except Exception as e:  # noqa: BLE001
                return self._send(
                    *_json({"ok": False, "error": str(e)}, 500)
                )

        if path == "/api/guard":
            try:
                r = self._guard_set(body)
                # ⚠️ 改完邮箱配置要**立刻清能力缓存**。
                #
                #    老爸实测（2026-11-05）：
                #        「我关掉邮箱，立刻点击严格防护，是可以点击的，
                #          但是过了一会儿不行；开启后也一样。」
                #
                #    原因：capabilities 缓存 30 秒。
                #    用户改完配置，界面还在用旧数据判按钮灰不灰。
                #    点完这一下就清，下次读立刻是新的。
                try:
                    from guard import capabilities as _cap2

                    _cap2.clear_cache()
                except Exception:  # noqa: BLE001
                    pass
                return self._send(*_json(r))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/guard/test":
            try:
                from guard import guard

                # ⚠️ 用**界面上填的**值发测试邮件，不用已保存的那份。
                #
                #    正常的顺序是"填 → 测 → 存"。
                #    但 send_code_email 原来一进来就 load_config()，
                #    读的是上一次保存的值——用户填了半天，
                #    测的却是旧配置，必然失败。
                #
                #    所以这里把界面上的值拼一份传进去。
                cur = guard.load_config()
                test_cfg = dict(cur)

                for k in ("email", "smtp_host", "smtp_user"):
                    v = self._str_field(body, k)
                    if v:
                        test_cfg[k] = v.strip()

                port = body.get("smtp_port")
                if isinstance(port, (int, str)) and str(port).strip():
                    try:
                        test_cfg["smtp_port"] = int(port)
                    except (TypeError, ValueError):
                        pass

                pw = self._str_field(body, "smtp_pass")
                if pw and pw.strip():
                    test_cfg["smtp_pass"] = pw.strip()

                # 服务器没填就按邮箱域名推一个
                if not test_cfg.get("smtp_host") and test_cfg.get("email"):
                    h, p_ = guard.detect_smtp(test_cfg["email"])
                    if h:
                        test_cfg["smtp_host"] = h
                        test_cfg["smtp_port"] = p_

                ok, msg = guard.send_code_email(
                    "这是一封测试邮件，不需要做任何事",
                    purpose="测试安全邮箱能不能收到",
                    config=test_cfg,
                )
                return self._send(*_json({"ok": ok, "message": msg}))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/guard/code":
            try:
                from guard import guard

                task = self._str_field(body, "task")
                purpose = self._str_field(body, "purpose")
                if not task:
                    return self._send(400, b'{"ok":false,"error":"task is required"}')
                if not guard.is_guard_ready():
                    return self._send(*_json({
                        "ok": False,
                        "message": "安全邮箱没配好。让用户去桌面端 设置 → 危险操作验证 里填。",
                    }))
                ok, msg = guard.send_code_email(task, purpose=purpose)
                return self._send(*_json({"ok": ok, "message": msg}, 200 if ok else 400))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/quiet":
            # 静默模式：验过码之后，把"要验证码的关卡"关一段时间。
            #
            # ⚠️ **开静默必须先验码。**
            #
            #    这是整套体系里最危险的一个操作 —— 它把五道关全关了。
            #    如果能不验码就开，那攻击者只要在聊天里说一句
            #    "帮我关一下验证码"就全绕过了。
            #
            #    所以这里强制要 code：
            #        code 来自 guard.verify() 那条路（用户填邮箱里的码）。
            #
            # ⚠️ 关多久**由用户说**，不是固定的 1h/4h/到今晚。
            #    前台把"关到下午五点"翻译成 seconds 再传进来。
            try:
                from guard import guard as _g
                from guard import quiet as _q

                action = self._str_field(body, "action", "start").strip() or "start"

                # ── 结束静默：不用码 ──
                #    恢复保护永远是安全的，不需要证明什么。
                if action == "stop":
                    ok, msg = _q.stop("手动恢复")
                    return self._send(*_json({"ok": ok, "message": msg, **_q.status()}))

                # ── 查看状态：不用码 ──
                if action == "status":
                    return self._send(*_json({"ok": True, **_q.status()}))

                # ── 开始静默：必须验码 ──
                code = self._str_field(body, "code").strip()
                seconds = body.get("seconds")

                if not code:
                    return self._send(*_json({
                        "ok": False,
                        "need_code": True,
                        "error": "开静默要先验证码。先把邮箱里的码填进来。",
                    }, 400))

                # 验码。用固定的 UNLOCK_TARGET 当目标 ——
                # 静默是全局的，不跟某个具体任务绑定。
                ok, msg = _g.unlock_with_code(code)
                if not ok:
                    return self._send(*_json({
                        "ok": False,
                        "error": "验证码不对",
                        "detail": msg,
                    }, 400))

                try:
                    secs = int(seconds or 0)
                except (TypeError, ValueError):
                    secs = 0

                if secs <= 0:
                    return self._send(*_json({
                        "ok": False,
                        "error": "码验过了，但没说关多久。告诉我要关到几点。",
                        "verified": True,
                    }, 400))

                ok2, msg2 = _q.start(secs, self._str_field(body, "why") or "用户验码后关闭验证")
                return self._send(*_json({
                    "ok": ok2,
                    "message": msg2,
                    **_q.status(),
                }, 200 if ok2 else 400))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500))

        if path == "/api/guard/check":
            try:
                from guard import guard

                task = self._str_field(body, "task")
                if not task:
                    return self._send(400, b'{"ok":false,"error":"task is required"}')
                r = guard.check(task)
                return self._send(*_json({"ok": True, **r}))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/guard/selftest":
            # 自检接口：在服务进程内跑一遍 guard 的完整状态机。
            #
            # 为什么需要它：
            #    guard 的状态在**服务进程的内存里**。
            #    测试脚本 import 的是另一份，两边内存不通——
            #    实测过好几次"测试说没锁、服务说锁着"，
            #    就是两份状态在打架。
            #
            #    这个接口让测试跑在**服务自己的那份状态里**，
            #    测完自己清理干净。
            #
            # 安全：
            #   · 需要 token（跟其他接口一样）
            #   · 只在本机
            #   · 只做自检，不接受"把某个任务设成已通过"这种参数
            #     （那才是真后门）
            try:
                return self._send(*_json(self._guard_selftest()))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/guard/unlock":
            # 专门解锁"任务端锁死"，不需要 task。
            #
            # ⚠️ 为什么要单独一个接口：
            #
            #     锁死时发的码，pending 记在一个固定目标上
            #     （guard.UNLOCK_TARGET），不跟具体任务绑定。
            #
            #     而 /api/guard/verify 要传 task，指纹对不上 →
            #     用户收得到码、填进去却永远验不过。
            #
            #     实测确认过这个断链：锁死的码是废的，
            #     用户没有任何办法自己解锁。
            try:
                from guard import guard

                code = self._str_field(body, "code").strip()
                if not code:
                    return self._send(400, b'{"ok":false,"error":"code is required"}')
                ok, msg = guard.unlock_with_code(code)

                if ok:
                    # ⚠️ 解锁之后，把扣着的删除真的做掉。
                    #
                    #    沙箱拦下的删除只记了"该删哪些"，
                    #    等用户验证码点头才真删。
                    #    原来 commit_pending_deletes 零调用 ——
                    #    用户给了码，那批删除永远不执行。
                    #
                    #    现在接在这里：解锁 = 用户点头。
                    try:
                        from engine import tools as _tools

                        _res = _tools.commit_pending_deletes()
                        if _res:
                            msg += f"\n（另外把 {len(_res)} 个待确认的删除做掉了。）"
                    except Exception as _e:  # noqa: BLE001
                        msg += f"\n（待确认的删除没做完：{type(_e).__name__}）"

                return self._send(*_json({"ok": ok, "message": msg}))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/guard/verify":
            try:
                from guard import guard

                task = self._str_field(body, "task")
                code = self._str_field(body, "code")
                ok, msg = guard.verify(task, code)
                return self._send(*_json({"ok": ok, "message": msg}))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/autostart":
            try:
                import paths

                ok, msg = paths.set_autostart(bool(body.get("on")))
                return self._send(*_json({
                    "ok": ok,
                    "enabled": paths.is_autostart(),
                    "note": msg,
                }))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/paths":
            try:
                st, b = _json(self._paths_set(body))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/start":
            target = self._str_field(body, "target").strip()
            try:
                st, b = _json(self._start(target))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/stop":
            target = (body.get("target") or "").strip()
            try:
                st, b = _json(self._stop(target))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/restart":
            target = (body.get("target") or "").strip()
            try:
                st, b = _json(self._restart(target))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/model":
            preset = (body.get("preset") or "").strip()
            effort = (body.get("effort") or "").strip()
            try:
                from tools import tune_model

                rec = tune_model.apply(preset, effort)
                st, b = _json(rec, 200 if rec.get("status") == "ok" else 400)
            except Exception as e:  # noqa: BLE001
                st, b = _json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/cancel":
            name = (body.get("name") or "").strip()
            if not name:
                return self._send(400, b'{"error":"name is required"}')
            try:
                st, b = _json(bridge.cmd_cancel(name))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        return self._send(404, b'{"error":"not found"}')

    # ── 具体处理 ──

    def _windows(self, path: str):
        """列出某套配置下面的所有窗口。

        前台（QQ/飞书那边的丫头）拿这个判断"这个活该不该放进已有窗口"。
        判断逻辑在前台，这里只负责把事实给她：
            · 窗口叫什么
            · 它开头那段需求是什么（方便她对照上下文）
            · 现在什么状态

        ?profile=xxx 只看那套；不给就看全部。
        """
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(path).query)
        want = (q.get("profile") or [""])[0]

        tasks = bridge._read_tasks()
        out = []
        for t in tasks:
            pid = t.get("profile") or ""
            if want and pid != want:
                continue
            out.append({
                "name": t.get("id") or "",
                "profile": pid,
                "status": t.get("status") or "",
                "task": (t.get("task") or "")[:300],
                "created_at": t.get("created_at") or "",
                "finished_at": t.get("finished_at") or "",
            })

        # 最近动过的排前面
        out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return self._send(*_json({"ok": True, "windows": out}))

    def _tasks(self):
        """任务列表：把 state 和 tasks.jsonl 合起来给前端。

        state 里有花费数据，tasks.jsonl 里有完整记录，两边合一下。
        """
        try:
            states = {s.get("id"): s for s in bridge.list_states()}
            tasks = bridge._read_tasks()
            out = []
            for t in tasks:
                tid = t.get("id", "")
                s = states.get(tid) or {}
                out.append({
                    "name": tid,
                    "status": s.get("status") or t.get("status"),
                    "task": t.get("task") or "",
                    "seconds": s.get("seconds") or t.get("seconds"),
                    # ⚠️ created_at 原来漏了。
                    #
                    #    tasks.jsonl 里明明记着它（派活那一刻的时间），
                    #    但这里没往上报，前端就拿不到"这条是什么时候派的"。
                    #    于是"最近的活"没法按时间排序，只能看返回顺序 ——
                    #    而返回顺序是按 updated_at 排的，
                    #    跑完的会跑到正在跑的前面，看着很乱。
                    #
                    #    （2026-11-05 老爸要"最新的几条放上面"才发现的。）
                    "created_at": t.get("created_at"),
                    "started_at": t.get("started_at"),
                    "finished_at": t.get("finished_at"),
                    "updated_at": s.get("updated_at") or t.get("updated_at"),
                    # 花费：只有跑过的任务才有
                    "context_tokens": s.get("context_tokens"),
                    "cost_cny": s.get("cost_cny"),
                    "result": s.get("result") or "",
                    "error": s.get("error") or "",

                    # ⚠️ 这三个字段原来没给，前端就没法"按模型分组"。
                    #
                    #    桌面上想看到的是：
                    #        DeepSeek  3/3   报告A write_file / 报告B 排队
                    #        Kimi      1/3   登录页 读文件
                    #
                    #    没有 profile，只能给一张扁平表——
                    #    用户和排查的人都看不出哪个任务跑在哪套模型上。
                    "profile": t.get("profile") or s.get("profile") or "",
                    "doing": s.get("doing") or "",
                    "turns": s.get("turns"),
                    "compactions": s.get("compactions"),
                })
            # ⚠️ 排序规则：**按派出时间倒序**（最新的在最前）。
            #
            #    原来是按 updated_at 排。问题：
            #        一条昨天派的老任务，今天重跑一下，
            #        updated_at 变成今天 —— 就排到最新的前面了。
            #        用户看到的是"最上面那条不是我刚派的"。
            #
            #    现在按 created_at。它不会变，就是"这条什么时候派的"。
            #    没有 created_at 的（老数据）退回 updated_at。
            out.sort(
                key=lambda x: x.get("created_at") or x.get("updated_at") or "",
                reverse=True,
            )
            self._send(*_json(out))
        except Exception as e:  # noqa: BLE001
            self._send(*_json({"error": f"{type(e).__name__}: {e}"}, 500))

    def _task(self, name: str):
        try:
            s = bridge.read_state(name)
            if not s:
                return self._send(*_json({"error": "not found"}, 404))
            self._send(*_json(s))
        except Exception as e:  # noqa: BLE001
            self._send(*_json({"error": f"{type(e).__name__}: {e}"}, 500))

    def _task_log(self, name: str):
        """这个任务的历史轮次。

        最后一轮在 state 里，完整历史在 tasks.jsonl。
        这里给的是 tasks.jsonl 里这个任务的那条（含最新一次结果）。
        """
        try:
            tasks = bridge._read_tasks()
            rec = next((t for t in tasks if t.get("id") == name), None)
            if not rec:
                return self._send(*_json({"error": "not found"}, 404))
            self._send(*_json(rec))
        except Exception as e:  # noqa: BLE001
            self._send(*_json({"error": f"{type(e).__name__}: {e}"}, 500))

    def _dispatch_with_concurrency(self, task: str, name: str,
                                   profile: str) -> dict:
        """派活，并按这套配置的并发额度决定立刻跑还是排队。

        为什么要排队而不是拒绝：
            用户派完就关窗口了，不会回来看"哎呀满了要重试"。
            排着队，等有空位后台自己会拉起来。

        状态用 queued，跟 pending 的区别：
            pending  刚派，还没轮到（通常马上就跑）
            queued   这套满了，明确在等
        """
        import engine.profiles as P
        import engine.scheduler as S

        # ⚠️ 顺序很重要：先看有没有空位，再派活。
        #
        #    原来反了：先 cmd_dispatch 写进去（状态 pending），
        #    再检查 has_slot。而 pending 是算占位的，
        #    等于"我自己刚占了一个位子，然后检查还有没有空位"——
        #    conc=1 的配置永远显示"满了"，第一个任务就排队，再也不会跑。
        #
        #    所以先定好用哪套、看它现在几个占位、还有没有位置，
        #    最后才真正写进去。
        pid = (profile or "").strip()
        if not pid:
            pid = (P.default_profile() or {}).get("id") or ""

        # ⚠️ 一套能用的模型都没有时，**这里就得拦住**。
        #
        #    原来照样派：任务写进去、worker 拉起来、
        #    然后因为没模型挂了（错误还被 DEVNULL 吞掉），
        #    状态停在 running —— 界面一直转圈。
        #
        #    实测：全新环境派活 → 状态 running → 永远转圈。
        #    新手等几分钟，关掉，卸载。
        #
        #    拦在这里，并把「该怎么办」说清楚。
        if not P.enabled_profiles():
            return {
                "error": "还没有能用的模型，活派出去也跑不了。",
                "hint": "去「设置」页点「＋ 新增模型」，"
                        "填接口地址、API Key、模型名，点「测试连接」通过了再「保存」。",
                "code": "no_model",
            }

        prof = P.get_profile(pid) if pid else {}
        limit = int((prof or {}).get("concurrency") or 1)

        has_room = S.has_slot(pid, limit) if pid else True

        rec = bridge.cmd_dispatch(task, name, pid)

        # ⚠️ 被安全闸拦下的，**在这里就要收手**。
        #
        #    原来不看 rec 是什么状态，直接按 has_room 决定起不起 worker：
        #        rec = cmd_dispatch(...)   ← 被拦，返回 blocked
        #        if has_room:
        #            self._spawn(name)     ← 照样起
        #
        #    而且 cmd_dispatch 被拦时**不写任务表**，
        #    于是 worker 起来去表里找 → 找不到 → 推一条"没跑成"。
        #
        #    用户看到的：
        #        [锁死测试_xxx] 没跑成：找不到这个任务
        #    实际发生的：被安全闸拦住了。
        #    两回事被说成了最让人困惑的那种。
        #
        #    实测（用户飞书截图）：锁死之后派一条，收到"找不到这个任务"。
        if rec.get("status") == "blocked" or rec.get("blocked"):
            return rec

        if has_room:
            self._spawn(name)
            return rec

        # 满了：标成排队
        bridge._update_task(name, status="queued")
        bridge.write_state(name, status="queued", profile=pid)
        rec["status"] = "queued"
        rec["queue_position"] = S.queue_position(pid, name)
        rec["queue_total"] = limit
        return rec

    def _profiles_get(self):
        """把全部模型配置交给界面。

        key 不回传原文，只给尾号——界面上不需要看到完整 key。
        """
        from engine.profiles import load_all

        data = load_all()
        out = []
        for prof in data.get("profiles") or []:
            key = prof.get("api_key") or ""
            out.append({
                "id": prof.get("id"),
                "name": prof.get("name"),
                "base_url": prof.get("base_url"),
                "model": prof.get("model"),
                "has_key": bool(key),
                "key_tail": key[-4:] if len(key) >= 4 else "",
                "max_steps": prof.get("max_steps") or 40,
                "enabled": prof.get("enabled", True),
                "concurrency": prof.get("concurrency", 1),
            })
        return self._send(*_json({
            "ok": True,
            "active": data.get("active") or "",
            "profiles": out,
        }))

    def _profiles_act(self, body: dict) -> dict:
        """加、改、删、切换模型配置。

        action 可以是：
            add     新增一套（需要 name / base_url / api_key / model）
            update  改一套（需要 id，其余字段可选）
            delete  删一套（需要 id）
            switch  切换到某套（需要 id）
        """
        from engine.profiles import (
            add_profile, delete_profile, set_active, update_profile,
        )

        action = (body.get("action") or "").strip()

        if action == "add":
            prof = add_profile(body)
            # add_profile 拦下来的时候不抛异常，只在返回值上挂 _error。
            # 这里把它翻成接口层看得懂的失败。
            err = prof.get("_error")
            if err:
                return {
                    "ok": False,
                    "error": err,
                    "duplicate_of": prof.get("_duplicate_of") or "",
                }
            return {"ok": True, "id": prof["id"]}

        if action == "update":
            pid = (body.get("id") or "").strip()
            if not pid:
                return {"ok": False, "error": "没说要改哪一套"}
            return {"ok": update_profile(pid, body)}

        if action == "delete":
            pid = (body.get("id") or "").strip()
            if not pid:
                return {"ok": False, "error": "没说要删哪一套"}
            return {"ok": delete_profile(pid)}

        if action == "switch":
            pid = (body.get("id") or "").strip()
            if not pid:
                return {"ok": False, "error": "没说要切到哪一套"}
            return {"ok": set_active(pid)}

        return {"ok": False, "error": f"不认识的动作：{action}"}

    def _engine_get(self):
        """任务端现在用哪个模型。

        key 不回传完整值，只回一个"配没配"和尾号——
        界面上不需要看到完整的 key，也不该看到。
        """
        try:
            from core import engine_bridge

            c = engine_bridge.load_config()
            key = c.get("api_key") or ""
            return self._send(*_json({
                "ok": True,
                "base_url": c.get("base_url") or "",
                "model": c.get("model") or "",
                "max_steps": c.get("max_steps") or 40,
                "has_key": bool(key),
                "key_tail": (key[-4:] if len(key) >= 4 else ""),
                "configured": engine_bridge.is_configured(),
                "config_path": engine_bridge.config_path(),
            }))
        except Exception as e:  # noqa: BLE001
            return self._send(*_json({"ok": False, "error": str(e)}, 500))

    def _engine_models(self, body: dict):
        """列出这个接口上可用的模型，给界面做下拉框。

        用 POST + JSON 传参，不用 query string。
        踩过：把 base_url 塞在 URL 里，里面的 "//" 在解析时丢了，
        结果一直读到已保存的旧地址，查出来的是另一家的模型列表。
        JSON 里传字符串没有这种坑。

        key 留空就用配置里存的——用户只改地址时不用重输。
        """
        from engine.models import list_models, pick_default

        base = (body.get("base_url") or "").strip()
        key = (body.get("api_key") or "").strip()

        # ⚠️ Key 的来源要按"用户正在改的那套配置"取。
        #
        #    原来这里拿的是 engine_bridge.load_config()——
        #    那是**旧的单套配置**，跟界面上多套列表不是一回事。
        #
        #    结果：用户在 Kimi 那套里填了 Key，点"查模型"
        #    拿到的还是旧那套的 Key，当然对不上。
        if not key:
            base, key = _profile_creds(body.get("id") or "", base)

        ok, models, err = list_models(base, key)
        if not ok:
            return self._send(*_json({"ok": False, "error": err}))

        # models 现在是 [(id, 显示名)]。
        #
        # 为什么分两份给前端：
        #     官方返回的 id 是 deepseek-flash，
        #     但 name 是 DeepSeek-V4.1-Flash。
        #     下拉框要显示好看的那个，调接口要用难看的那个。
        #     前端用 QComboBox 的 setItemData 把 id 挂在每一项上。
        ids = [m[0] for m in models]
        labels = [m[1] for m in models]

        return self._send(*_json({
            "ok": True,
            "ids": ids,
            "labels": labels,
            # 老格式也留着：别的地方可能还在用
            "models": ids,
            "default": pick_default(models),
        }))

    def _engine_set(self, body: dict) -> dict:
        """保存模型配置。

        如果没传 api_key（界面上留空表示"不改"），
        就保留原来的 key——不能让用户为了改个地址把 key 重打一遍。
        """
        from core import engine_bridge

        cur = engine_bridge.load_config()
        base = (body.get("base_url") or "").strip()
        model = (body.get("model") or "").strip()
        key = (body.get("api_key") or "").strip()

        cur["base_url"] = base
        cur["model"] = model
        if key:
            cur["api_key"] = key
        try:
            cur["max_steps"] = int(body.get("max_steps") or cur.get("max_steps") or 40)
        except (TypeError, ValueError):
            cur["max_steps"] = 40

        engine_bridge.save_config(cur)
        return {
            "ok": True,
            "configured": engine_bridge.is_configured(),
        }

    def _engine_test(self, body: dict) -> dict:
        """真的发一次请求，确认这套配置能用。

        为什么要在界面上放这个：
            用户填完不知道自己填对没有。等派了一个活失败了才
            发现地址写错，那就太晚了。

        ## ⚠️ 没带 Key 的时候，要找**对**那个Key

            老爸实测（2026-11-05）：
                「审计员那边为啥我填写测试是通了，
                  但是保存后再测试，返回 401？」

            原因链：
                ① 测试请求 body 里带的是**界面上的** Key
                ② 保存之后界面把 Key 框清空（防复制，只留"尾号 xxxx"）
                ③ 再点测试 → body 里没有 api_key
                ④ 这里去 _profile_creds() 补 —— 那个函数找的是
                   **任务端模型**的 Key（active 那套）
                ⑤ 拿 DeepSeek 的 Key 去连硅基流动的地址 → **401**

            所以补 Key 的时候得先按 base_url 找：
                这个地址是不是审计员配的那个？是就用审计员的 Key。
                （用户改的那个输入框地址跟已保存的一致，说明还是同一家。）
        """
        from engine import test_connection

        base = (body.get("base_url") or "").strip()
        model = (body.get("model") or "").strip()
        key = (body.get("api_key") or "").strip()

        # 没带 Key：先看这个地址是不是审计员那条线
        if not key and base:
            key = _auditor_key_for(base)

        # 再兜底到"正在改的那套"模型配置
        if not key:
            base2, key = _profile_creds(body.get("id") or "", base)
            base = base or base2

        if not base or not model or not key:
            return {"ok": False, "error": "三样都得填：接口地址、Key、模型名"}

        ok, msg = test_connection(base, key, model)
        return {"ok": ok, "message": msg}

    def _guard_set(self, body: dict) -> dict:
        """保存安全邮箱配置。

        ⚠️ 授权码不回传。
            传空表示"不改"——用户不想为了改个邮箱把授权码重打一遍。
        """
        from guard import guard

        cur = guard.load_config()
        for k in ("email", "smtp_host", "smtp_user"):
            v = self._str_field(body, k)
            if v:
                cur[k] = v.strip()

        port = body.get("smtp_port")
        if isinstance(port, (int, str)) and str(port).strip():
            try:
                cur["smtp_port"] = int(port)
            except (TypeError, ValueError):
                pass

        # 授权码：空 = 不改
        pw = self._str_field(body, "smtp_pass")
        if pw and pw.strip():
            cur["smtp_pass"] = pw.strip()

        cur["enabled"] = bool(body.get("enabled", True))
        guard.save_config(cur)

        return {
            "ok": True,
            "ready": guard.is_guard_ready(),
            "email_masked": guard._mask_email(cur.get("email") or ""),
        }

    def _guard_selftest(self) -> dict:
        """在服务进程里跑一遍 guard 的完整状态机。

        为什么在这里跑而不是在外面跑：
            guard 的状态在进程内存里。测试脚本 import 的是另一份，
            两边不通——实测过好几次"测试说没锁、服务说锁着"。

            这个函数跑在服务自己的进程里，用的是同一份状态。

        会做什么：
            塞临时的 pending / 锁，验证状态机对不对，然后清干净。
            **不会发真邮件**（`send_code_email` 会发信，这里绕开它，
            直接构造 PendingCode）。

        清理是 finally 保证的——即使断言中途抛了，也会清干净。
        这一点很要紧：围栏没清的话，后面所有派活都会被挡，
        用户会以为程序坏了。
        """
        import time as _t

        from guard import guard

        results = []

        def ck(desc: str, got, want) -> None:
            ok = (got in want) if isinstance(want, (tuple, list)) else (got == want)
            results.append({"desc": desc, "ok": ok, "got": got, "want": want})

        def reset() -> None:
            guard._state.pending.clear()
            guard._state.locked_until.clear()
            guard._state.fails.clear()
            guard._locked.clear()
            guard._force_locked.clear()
            guard._verified.clear()

        try:
            reset()

            # ── A. 平时 ──
            r = guard.check("把按钮改大")
            ck("平时 fenced=False", r["fenced"], False)
            ck("平时可以派", r["verified"], True)

            # ── B. 发码 → 全站锁 ──
            danger = "__selftest__ 删掉用户表"
            fp = guard._fingerprint(danger)
            guard._state.pending[fp] = guard.PendingCode(
                code="123456", task=danger, purpose="", expires=_t.time() + 120
            )
            r = guard.check(danger)
            ck("危险任务被锁", r["fenced"], True)
            ck("在等用户填", r["waiting"], True)
            ck("可以重发码", r["can_resend"], True)

            r = guard.check("把按钮改大")
            ck("★ 普通任务也被锁", r["fenced"], True)

            # ── C. 验证通过 → 解锁 ──
            ok, _ = guard.verify(danger, "123456")
            ck("验证通过", ok, True)
            r = guard.check(danger)
            ck("危险任务解锁", r["fenced"], False)
            r = guard.check("把按钮改大")
            ck("★ 普通任务解锁", r["fenced"], False)

            # 一次性
            ok2, _ = guard.verify(danger, "123456")
            ck("验证码不能复用", ok2, False)

            # ── D. 过期 → 锁 10 分钟 ──
            reset()
            guard._state.pending[fp] = guard.PendingCode(
                code="111111", task=danger, purpose="", expires=_t.time() - 1
            )
            r = guard.check(danger)
            ck("过期后仍锁", r["fenced"], True)
            ck("锁约 600 秒", 580 < r["lock_left"] <= 600, True)
            ck("普通过期可以重发", r["can_resend"], True)

            # ── E. 错三次 → 强制锁 ──
            reset()
            guard._state.pending[fp] = guard.PendingCode(
                code="222222", task=danger, purpose="", expires=_t.time() + 120
            )
            o1, m1 = guard.verify(danger, "000000")
            o2, m2 = guard.verify(danger, "000000")
            o3, m3 = guard.verify(danger, "000000")
            ck("第1次错被拒", o1, False)
            ck("提示还剩2次", "2" in m1, True)
            ck("第2次错被拒", o2, False)
            ck("提示还剩1次", "1" in m2, True)
            ck("第3次错被拒", o3, False)

            r = guard.check(danger)
            ck("错三次后强制锁", r["forced"], True)
            ck("强制锁不许重发", r["can_resend"], False)

            ok4, msg4 = guard.send_code_email(danger, "")
            ck("强制锁期间重发被拒", ok4, False)

            r = guard.check("普通任务")
            ck("强制锁期间普通任务也锁", r["fenced"], True)

            # ── F. 锁满 → 自动解 ──
            for k in list(guard._force_locked.keys()):
                guard._force_locked[k] = _t.time() - 1
            for k in list(guard._locked.keys()):
                guard._locked[k] = _t.time() - 1
            guard._state.locked_until.clear()
            r = guard.check("普通任务")
            ck("锁满自动解", r["fenced"], False)
            ck("可以重发码了", r["can_resend"], True)

        finally:
            # 不管上面成没成，都清干净
            reset()

        passed = sum(1 for x in results if x["ok"])
        return {
            "ok": passed == len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "results": results,
        }

    # ── 权限 ──
    def _perms_get(self) -> dict:
        """列出每个窗口的权限。"""
        from guard import task_perms as tp

        cfg = tp.load()
        tasks = []
        try:
            from core import bridge as _b

            for t in _b._read_tasks():
                nm = t.get("id") or ""
                if not nm:
                    continue
                tasks.append({
                    "name": nm,
                    "status": t.get("status"),
                    "level": tp.get_level(nm),
                    # 加 profile：前端要按模型分组显示。
                    # 原来没这个字段，前台只能给一张扁平表，
                    # 分不清哪个窗口跑在哪套模型上。
                    "profile": t.get("profile") or "",
                })
        except Exception:  # noqa: BLE001
            pass

        tasks.sort(key=lambda x: str(x["name"]))

        return {
            "ok": True,
            "levels": list(tp.LEVELS),
            "labels": tp.LEVEL_LABEL,
            "hints": tp.LEVEL_HINT,
            "default": "workspace",
            "tasks": tasks,
            "custom": cfg,
        }

    def _perms_set(self, body: dict) -> dict:
        """改一个窗口的权限。

        body:
            {"name": "任务名", "level": "workspace|home|full", "code": "可选"}

        ⚠️ **升权必须过验证码。**

            为什么：
                权限档位是攻击者唯一需要拿下的那一个点。

                如果他能在对话里直接说"帮我把那个窗口的权限调一下"，
                那整套围栏就白做了——他不需要绕过围栏，
                他只要让前台把围栏拆了。而且这个请求会伪装成
                一次完全正常的远程办公操作。

            分两种情况：
                降权（收窄）  直接改。因为它只会让人更安全。
                升权（放宽）  必须先过码，一次一码。

            码绑的是"给『某个任务名』开『某个级别』"这一件事，
            不能拿去干别的，也不能换个任务名复用。
        """
        from guard import task_perms as tp

        name = (body.get("name") or "").strip()
        level = (body.get("level") or "").strip().lower()
        code = (body.get("code") or "").strip()

        if not name:
            return {"ok": False, "error": "没说是哪个窗口"}
        if level not in tp.LEVELS:
            return {"ok": False, "error": f"不认识的权限级别：{level}"}

        # 只能改点名的**一个**窗口。
        #
        # 通配符、批量，一律不认——
        # "一次只开一扇门"是这套设计的前提。要是能一次开一片，
        # 那攻击者只要成功一次就等于全开了。
        if any(ch in name for ch in ("*", "?", "%")):
            return {
                "ok": False,
                "error": "一次只能改一个窗口，不能批量",
            }

        cur = tp.get_level(name)
        rank = {"workspace": 0, "home": 1, "full": 2}
        widening = rank.get(level, 0) > rank.get(cur, 0)

        purpose = f"perm:{name}:{level}"

        if widening:
            # 升权：必须有码
            try:
                from guard import guard
            except ImportError:
                return {"ok": False, "error": "guard 模块不可用，没法验证"}

            if not code:
                # 第一次来：发码
                ok, msg = guard.send_code_email(purpose, "改权限")
                return {
                    "ok": False,
                    "need_code": True,
                    "name": name,
                    "from": cur,
                    "to": level,
                    "message": msg if ok else f"发码失败：{msg}",
                }

            # 带码来了：验
            ok, msg = guard.verify(purpose, code)
            if not ok:
                return {
                    "ok": False,
                    "need_code": True,
                    "error": msg,
                }

        if not tp.set_level(name, level):
            return {"ok": False, "error": "没改成功"}

        return {
            "ok": True,
            "name": name,
            "from": cur,
            "level": level,
            "label": tp.LEVEL_LABEL.get(level, level),
            "hint": tp.LEVEL_HINT.get(level, ""),
            "verified": widening,
        }

    def _paths_get(self):
        """把路径配置交给设置页。

        返回两组：
            values  当前用的值（用户配过的或探测到的）
            detected 这一次探测到的（给"重新探测"按钮用）
        """
        import paths

        data = paths.load()
        # 空着的字段顺手补一下探测结果，但先不落盘——
        # 用户点"保存"才算数，免得在别的盘留下痕迹
        filled = paths.merge_detected(data)
        return self._send(*_json({
            "ok": True,
            "values": filled,
            "detected": paths.detect_all(),
            "missing": paths.missing(filled),
            "labels": paths.LABELS,
            "config_path": paths.config_path(),
        }))

    def _paths_set(self, body: dict) -> dict:
        """保存路径配置。只接受认识的键。"""
        import paths

        data = paths.load()
        for k in paths.DEFAULTS:
            if k in body and isinstance(body[k], str):
                data[k] = body[k].strip()
        paths.save(data)

        # 保存完让 launcher 重新读一遍
        global _launcher
        if _launcher is not None:
            try:
                _launcher.refresh_paths()
            except Exception:  # noqa: BLE001
                pass

        return {"ok": True, "missing": paths.missing(data)}

    def _detect_paths(self):
        """重新探测一遍。"""
        import paths

        found = paths.detect_all()
        return {"ok": True, "detected": found}

    def _qrcode(self):
        """把 NapCat 生成的登录二维码给桌面端。

        为什么需要这个：
            QQ 重启后如果登录态丢了，NapCat 会在 QQ 窗口里画二维码等人扫。
            但那个窗口在后台、或者干脆没显出来，用户根本看不见。
            实测过：用户看到的是"重启中…"，其实程序在等扫码，
            两边都僵在那儿。

            NapCat 会把二维码落盘到 cache/qrcode.png，
            所以直接读这个文件交给界面显示（base64），
            用户在自己屏幕上就扫得到。
        """
        import base64
        import glob

        # 二维码可能在不同版本的目录下，找最新的一张
        patterns = _qrcode_patterns()
        files = []
        for pat in patterns:
            files.extend(glob.glob(pat))
        if not files:
            return self._send(*_json({"ok": False, "error": "还没有二维码"}))

        newest = max(files, key=os.path.getmtime)
        age = time.time() - os.path.getmtime(newest)
        # QQ 的登录码大约 3 分钟有效。留 15 秒余量，
        # 免得用户刚看到"还剩 3 秒"就过期了。
        ttl = 180
        left = max(0, ttl - age)
        if left <= 5:
            return self._send(*_json({
                "ok": False,
                "error": "二维码已经过期了",
                "left": 0,
            }))

        try:
            raw = open(newest, "rb").read()
        except OSError as e:
            return self._send(*_json({"ok": False, "error": f"读不了二维码：{e}"}))

        return self._send(*_json({
            "ok": True,
            "png": base64.b64encode(raw).decode("ascii"),
            "age": int(age),
            "left": int(left),      # 还剩多少秒
            "ttl": ttl,
        }))

    @staticmethod
    def _clear_qrcode() -> None:
        """登录成功后把二维码文件删掉。

        留在那儿有两个问题：
            · 界面下次打开还显示一张已经没用的码
            · 那个文件里有这次登录的临时凭证
        """
        import glob

        for pat in _qrcode_patterns():
            for f in glob.glob(pat):
                try:
                    os.remove(f)
                except OSError:
                    pass

    def _model_get(self):
        """当前任务端用的模型。设置页开的时候要看。"""
        try:
            from tools import tune_model

            cur = tune_model.get_current()
            return self._send(*_json({
                "preset": cur.get("preset") if cur.get("preset") not in (None, "(未知组合)") else "",
                "provider_now": cur.get("provider"),
                "model_now": cur.get("model"),
                "effort_now": cur.get("reasoning_effort"),
            }))
        except Exception as e:  # noqa: BLE001
            return self._send(*_json({"error": f"{type(e).__name__}: {e}"}, 500))

    def _ensure_host(self) -> dict:
        """确保宿主（AstrBot）在跑。两条通道都要它。"""
        if port_open(ASTRBOT_PORT, timeout=1):
            return {"ok": True}

        if _launcher is None:
            return {"ok": False, "error": "启动模块没加载上"}

        ok, msg = _launcher.start_bot()
        if not ok:
            return {"ok": False, "error": msg}

        import time as _t

        for _ in range(100):
            _t.sleep(1)
            if port_open(ASTRBOT_PORT, timeout=0.5):
                # 端口通了还不够，插件的启停接口也要就绪
                try:
                    from desktop import channels as _ch

                    _ch.wait_ready(timeout=60)
                except Exception:  # noqa: BLE001
                    pass
                return {"ok": True}

        return {"ok": False, "error": "对话服务没起来，可能启动报错了。"}

    def _set_channel(self, platform_id: str, on: bool) -> dict:
        """在 AstrBot 里打开或关掉一条通道（热操作，不重启）。"""
        try:
            from desktop import channels as _ch

            if on:
                return _ch.enable(platform_id)
            return _ch.disable(platform_id)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _ensure_channel(self, platform_id: str) -> dict:
        """确保某条通道是开着的。"""
        r = self._set_channel(platform_id, True)
        return r

    def _start_feishu(self) -> dict:
        """只开飞书：起宿主 + 关掉 QQ 平台。"""
        host = self._ensure_host()
        if not host.get("ok"):
            return host

        # 打开飞书
        r = self._set_channel("feishu", True)
        if not r.get("ok"):
            return {"ok": False, "error": f"打开飞书通道失败：{r.get('error')}"}

        # 顺手把 QQ 平台关掉——用户点的是"只开飞书"。
        # 注意只关连接，不杀 QQ 客户端：
        # 那是用户自己的 QQ，随便杀会让他正在用的微信/QQ 全断。
        self._set_channel("qq-napcat", False)

        return {"ok": True, "note": "飞书通道开了（没带 QQ，省内存）。"}

    def _start(self, target: str) -> dict:
        """启动一个服务。全程不弹任何窗口。

        为什么要等端口：
            光看进程起来了不算数。QQ 起来了但 NapCat 没注入进去、
            AstrBot 起来了但适配器没连上，都会卡在半路，
            用户看到"已启动"却发现用不了。
            所以这里一直等到端口真的通（或超时）再回话。
        """
        if _launcher is None:
            return {"ok": False, "error": "启动模块没加载上"}

        if target == "qq":
            # 先看 3000 通不通。通了才是真登录好了。
            if port_open(QQ_PORT, timeout=1):
                self._clear_qrcode()
                return {"ok": True, "note": "QQ 已经在跑了。"}

            # 进程在、但端口不通 → 起了没登录，或者注入失败。
            # 这种情况不要重开进程，直接让界面去扫码。
            #
            # ⚠️ 原来直接调 tasklist（Windows 专用）。
            #    而且只判了"有没有"，没判个数。
            #    现在交给 platform_utils，两个平台都能查。
            try:
                from tools import platform_utils as _pu

                cnt = 1 if _pu.process_running("QQ") else 0
            except ImportError:
                cnt = 0

            if cnt > 0:
                return {
                    "ok": True,
                    "need_login": True,
                    "note": "QQ 开着但没登录，扫一下码。",
                }

            ok, msg = _launcher.start_qq()
            if not ok:
                return {"ok": False, "error": msg}
            if port_open(QQ_PORT, timeout=1):
                # NapCat 通了，再把 AstrBot 里这条连接打开
                self._ensure_channel("qq-napcat")
                return {"ok": True, "note": "QQ 已经起来了。"}
            # 等最多 90 秒
            import time as _t

            for _ in range(90):
                _t.sleep(1)
                if port_open(QQ_PORT, timeout=0.5):
                    # 起来了，把通道打开、飞书关掉
                    self._ensure_host()
                    self._set_channel("qq-napcat", True)
                    self._set_channel("feishu", False)
                    return {"ok": True, "note": "QQ 起来了，NapCat 也接上了。"}
            # 要扫码 —— 但通道的事也得处理完。
            # 踩过：这里提前 return，飞书那条没关掉，
            # 用户点了「启动 QQ」，结果两个都开着。
            self._ensure_host()
            self._set_channel("qq-napcat", True)
            self._set_channel("feishu", False)
            return {
                "ok": True,
                "need_login": True,
                "note": "QQ 起来了，但还要扫码登录。",
            }

        if target == "feishu":
            # 飞书通道很轻：只需要宿主在跑，然后把这条连接打开。
            #
            # 跟原来不同的是：不再顺带把 QQ 也拉起来。
            # 只用飞书的人不该背着 QQ 客户端那几百兆。
            return self._start_feishu()

        if target == "bot":
            # 老接口名留着，转给 feishu
            return self._start_feishu()

        if target == "bridge":
            return {"ok": True, "note": "任务端不用单独启动，派活时自己会起。"}

        return {"ok": False, "error": f"不认识的服务：{target}"}

    def _stop(self, target: str) -> dict:
        if _launcher is None:
            return {"ok": False, "error": "启动模块没加载上"}

        if target == "qq":
            ok, msg = _launcher.stop_qq()
            return {"ok": ok, "note": msg}
        if target == "bot":
            ok, msg = _launcher.stop_bot()
            return {"ok": ok, "note": msg}
        if target == "bridge":
            return {"ok": True, "note": "任务端没有常驻进程。"}
        return {"ok": False, "error": f"不认识的服务：{target}"}

    def _restart(self, target: str) -> dict:
        """重启某个服务。

        为什么放在这里而不是让用户自己去开窗口：
            不懂电脑的人找不到那个黑窗口，也不知道该按什么顺序启动。

        重启 QQ 要走完整的关闭 + 重新注入流程，
        不能直接杀——杀了之后 NapCat 就注入不进去了。
        """
        import subprocess

        if target == "qq":
            # 重启 QQ 比看起来麻烦，这里踩过三个坑：
            #
            #   1) 只 taskkill QQ.exe 收不干净。
            #      QQ 会拉一串子进程（实测十几个），父进程没了，
            #      子进程还攥着内存共享段，NapCat 就注入不进去。
            #      必须先杀掉所有 QQ 相关进程，等它们真的消失。
            #
            #   2) 杀完立刻拉起会失败。要轮询确认进程数归零再启动，
            #      固定 sleep 2 秒不够。
            #
            #   3) 不能直接返回"正在重启"就完事。
            #      用户点完按钮得知道到底成没成，所以这里等
            #      3000 端口真的起来（或超时）再回话。
            # ⚠️ 重启 QQ / NapCat 是 Windows 专属功能。
            #
            #    NapCat 靠往 QQ 进程里注入 DLL 工作，
            #    而下面要调的 NapCatWinBootMain.exe 是 Windows 可执行文件，
            #    taskkill / tasklist 也是 Windows 命令。
            #
            #    Linux 上这套东西根本不存在。与其"适配"，不如直接说清楚——
            #    一个在 Linux 上假装能重启 QQ 的按钮，比没有这个按钮更糟。
            from tools import platform_utils as _pu_restart

            if not _pu_restart.IS_WINDOWS:
                return {
                    "ok": False,
                    "error": (
                        "重启 QQ / NapCat 只能在 Windows 上用。"
                        f"当前是 {_pu_restart.describe()}——"
                        "这个平台上没有 NapCat 的注入宿主。"
                    ),
                }

            import paths as _p

            nap = _p.get("napcat_dir")
            launcher = os.path.join(nap, "NapCatWinBootMain.exe") if nap else ""
            if not launcher or not os.path.exists(launcher):
                return {"ok": False, "error": "还没设置 NapCat 在哪，去设置页选一下"}

            def _qq_count() -> int:
                try:
                    out = subprocess.run(
                        ["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/NH"],
                        capture_output=True, timeout=15,
                    ).stdout.decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    return 0
                return out.count("QQ.exe")

            # 1) 杀掉所有 QQ 进程
            try:
                subprocess.run(["taskkill", "/F", "/T", "/IM", "QQ.exe"],
                               capture_output=True, timeout=25)
            except Exception:  # noqa: BLE001
                pass

            # 2) 等进程真的走干净（最多 20 秒）
            gone = False
            for _ in range(40):
                time.sleep(0.5)
                if _qq_count() == 0:
                    gone = True
                    break
            if not gone:
                return {
                    "ok": False,
                    "error": "QQ 关不掉，可能还有窗口没退。手动关掉 QQ 再试一次。",
                }

            # 3) 拉起来
            subprocess.Popen(
                ["cmd", "/c", launcher],
                cwd=os.path.dirname(launcher),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

            # 4) 等 3000 端口起来（最多 90 秒）
            for _ in range(90):
                time.sleep(1)
                if _port_ok(_NAPCAT_PORT):
                    # 登录成功，二维码没用了，顺手删掉
                    self._clear_qrcode()
                    return {"ok": True, "note": "QQ 已经起来了，登录也接上了。"}
            return {
                "ok": False,
                "error": "QQ 进程起来了，但 NapCat 没接上（3000 端口不通）。"
                         "可能要在 QQ 窗口里手动登录一下。",
            }

        if target == "bot":
            ports = _listening_pids(_BOT_PORT)
            for pid in ports:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, timeout=20)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(2)
            if _launcher is None:
                return {"ok": False, "error": "启动模块没加载上"}
            ok, msg = _launcher.stop_bot()
            ok2, msg2 = _launcher.start_bot()
            if not ok2:
                return {"ok": False, "error": msg2}
            return {"ok": True, "note": msg2}
            subprocess.Popen(["cmd", "/c", "start", "", bat],
                             cwd=os.path.dirname(bat), shell=False)
            return {"ok": True, "note": "对话服务正在重启，大约 30 秒。"}

        if target == "bridge":
            return {"ok": True, "note": "任务端是每次派活时新起的，不用重启。"}

        return {"ok": False, "error": f"不认识的服务：{target}"}

    def _outbox(self):
        try:
            rows = []
            if os.path.exists(bridge.OUTBOX):
                for line in open(bridge.OUTBOX, encoding="utf-8"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not r.get("delivered"):
                        rows.append(r)
            self._send(*_json(rows[-50:]))
        except Exception as e:  # noqa: BLE001
            self._send(*_json({"error": f"{type(e).__name__}: {e}"}, 500))

    @staticmethod
    def _spawn(name: str) -> None:
        """起一个后台进程真干活，不等它。"""
        import subprocess

        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        try:
            subprocess.Popen(
                [sys.executable, os.path.join(BRIDGE_DIR, "core", "bridge.py"),
                 "run", "--id", name, "--timeout", "1800"],
                cwd=BRIDGE_DIR, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    global _API_TOKEN

    from guard import security as _sec

    ap = argparse.ArgumentParser(description="桥的 HTTP 接口")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    # ⚠️ 没有 --host 参数了。
    #
    #    原来可以传 --host 0.0.0.0 把接口开给整个局域网，
    #    等于把"能派活、能读写文件、能跑命令"的手伸到网上。
    #    这个程序是给本机用的，地址写死。
    args = ap.parse_args()

    _API_TOKEN = _sec.load_or_create_token("dsh-bridge")

    # 先把"上辈子"留下的烂摊子收拾掉。
    #
    # ⚠️ 为什么要做这个：
    #     进程被强杀、机器断电、桥崩溃——这些情况下进程没了，
    #     但 tasks.jsonl 里还写着 status="running"。
    #     界面就一直转圈，用户以为它还在跑。
    #
    #     实测有一次一条任务从 10:51 挂到第二天，界面还显示"运行中"。
    #     用户看到的是"被锁的活 运行中"——那条其实是另一条已取消的任务，
    #     但底下这条真卡死的在列表里，看着就是没跑完。
    try:
        from core import bridge as _bridge
        from core import recover as _recover

        _fixed = _recover.recover(
            workspaces=os.path.join(BRIDGE_DIR, "workspaces"),
            read_tasks=_bridge._read_tasks,
            write_tasks=_bridge._write_tasks,
            now_iso=_bridge._now(),
        )
        if _fixed:
            print(f"[api] 收回 {len(_fixed)} 条卡住的任务：")
            for it in _fixed:
                print(f"       {it['id']} —— {it['why']}")
        else:
            print("[api] 没有卡住的任务")
    except Exception as e:  # noqa: BLE001
        print(f"[api] 清理卡住任务失败（不影响启动）：{type(e).__name__}: {e}")

    # 起一个后台线程，定期把排队中的任务拉起来。
    #
    # 为什么放在接口服务里而不是桥里：
    #   桥是一次性的（跑完就退出），接口服务是常驻的。
    #   排队轮转需要一个一直在的东西来做。
    try:
        import engine.scheduler as S

        S.start_pump_in_background(interval=5.0)
        print("[api] 排队轮转已启动（每 5 秒检查空位）")
    except Exception as e:  # noqa: BLE001
        print(f"[api] 排队轮转没起来：{e}")

    srv = ThreadingHTTPServer((_sec.HOST, args.port), Handler)
    print(f"[api] 桥接口已启动：http://{_sec.HOST}:{args.port}")
    print(f"[api] 只接受本机连接，请求需带 X-Token")
    print(f"[api] token 在：{_sec.token_path('dsh-bridge')}")
    print(f"[api] 任务目录：{BRIDGE_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[api] 退出")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
