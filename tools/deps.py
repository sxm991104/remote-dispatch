"""运行环境检查与依赖安装。

为什么要有这个
    别人 clone 下来，跑不起来的原因往往不是代码有问题，
    而是缺东西：没装 PySide6、没装 python-docx、Python 版本太老。

    让人去翻文档、敲 pip install 是很劝退的。
    所以把这个做进桌面端：
        看一眼缺什么 → 点一下 → 自动装 → 进度条走给你看

设计上的两个决定
    一、只检查不强制
        缺 python-docx 只是"读不了 Word"，桥本身照样能跑。
        所以标成"可选"，不能因为缺它就拦着用户用。

    二、安装走 pip，不自己下载 whl
        pip 处理依赖、轮子、平台匹配都在行。
        自己拼 URL 下 whl 等于重造一遍，还容易出错。
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import sysconfig
import threading
import urllib.error
import urllib.request

# 各平台该把配置放哪儿，见 tools/userdirs.py。
# ⚠️ 这个 import 是补的 —— 之前把 APPDATA 换成 userdirs 时，
#    调用处换了，import 忘了加。
#    NameError 只在切到「环境」页时才炸，所以一直没被发现。
import tools.userdirs as _userdirs

# ── 依赖清单 ────────────────────────────────────────────
#
# 每一条：
#   key       内部名字
#   label     显示给人看的名字
#   module    导入时用的模块名（判断装没装）
#   pip       pip 装的时候用的包名
#   why       为什么要它
#   needed    True = 没有就跑不起来；False = 没有只是少个功能

DEPS = [
    {
        "key": "python",
        "label": "Python",
        "module": "",
        "pip": "",
        "why": "整个项目的基础",
        "needed": True,
        "is_python": True,
    },
    {
        "key": "pyside6",
        "label": "PySide6",
        "module": "PySide6",
        "pip": "PySide6",
        "why": "桌面端的界面",
        "needed": True,
    },
    {
        "key": "docx",
        "label": "python-docx",
        "module": "docx",
        "pip": "python-docx",
        "why": "读 Word 文档",
        "needed": False,
    },
    {
        "key": "openpyxl",
        "label": "openpyxl",
        "module": "openpyxl",
        "pip": "openpyxl",
        "why": "读 Excel 表格",
        "needed": False,
    },
    {
        "key": "pptx",
        "label": "python-pptx",
        "module": "pptx",
        "pip": "python-pptx",
        "why": "读 PPT",
        "needed": False,
    },
    {
        "key": "pypdf",
        "label": "pypdf",
        "module": "pypdf",
        "pip": "pypdf",
        "why": "读 PDF",
        "needed": False,
    },
]

MIN_PYTHON = (3, 10)


# ── 检查 ────────────────────────────────────────────────

def _module_installed(name: str) -> tuple[bool, str]:
    """这个模块能导入吗。返回 (装没装, 版本号)。"""
    if not name:
        return False, ""
    try:
        m = importlib.import_module(name)
        ver = getattr(m, "__version__", "") or getattr(m, "VERSION", "")
        if isinstance(ver, tuple):
            ver = ".".join(str(x) for x in ver)
        return True, str(ver)
    except ImportError:
        return False, ""
    except Exception:  # noqa: BLE001
        # 装是装了，但导入时炸了（通常是依赖缺或者 ABI 不匹配）
        return False, "装错了"


def check_python() -> dict:
    """Python 版本够不够。"""
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PYTHON
    return {
        "key": "python",
        "label": "Python",
        "installed": ok,
        "version": f"{v.major}.{v.minor}.{v.micro}",
        "why": f"需要 {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上",
        "needed": True,
        "can_install": False,        # Python 没法从桌面端装
        "note": "" if ok else "版本太老，请去 python.org 装新的",
    }


def check_bridge() -> dict:
    """咱们自己的桥在不在、能不能用。

    ## ⚠️ 桥不是 pip 包

        DEPS 里那些都是 pip 能装的第三方包，导进来就能验。
        桥是**这个仓库自己** —— 它已经在这儿了（你现在跑的就是它），
        所以这项永远"装好了"。

        那为什么还要列？

            因为它后面还有一半东西**不一定齐**：
                模型配了没
                验证码邮箱配了没
                端口通不通

            用户看依赖列表，想确认的是"能不能干活"，
            不是"pip 装没装"。只列 pip 包等于只回答了一半。

    ## 这项查什么

        · 仓库文件在不在（bridge 的入口）
        · 模型配置有没有
        · 这几样齐了才算"能干活"
    """
    import os

    # 桥的入口就在项目根下
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    has_core = os.path.exists(os.path.join(root, "core", "bridge.py"))
    has_api = os.path.exists(os.path.join(root, "server", "api_server.py"))

    ok = has_core and has_api

    note = ""
    if ok:
        from tools import userdirs

        # 顺手看一眼模型配了没 —— 没配模型等于派不了活
        try:
            eng = userdirs.config_file("engine.json")
            with open(eng, encoding="utf-8") as f:
                ej = json.load(f)
            profs = [p for p in (ej.get("profiles") or []) if p.get("enabled", True)]
            if profs:
                note = f"{len(profs)} 套模型"
            else:
                note = "还没配模型"
                ok = False
        except (OSError, json.JSONDecodeError, AttributeError):
            note = "还没配模型"
            ok = False

    return {
        "key": "bridge",
        "label": "桥（本项目）",
        "installed": ok,
        "version": _read_version(),
        "why": "派活、跑任务、验证码都归它",
        "needed": True,
        "can_install": False,
        "note": note,
    }


def _read_version() -> str:
    """读仓库根的 VERSION。"""
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _find_atrium() -> str:
    """atrium 装在哪。

    atrium 是个独立的仓库/包，不一定跟桥在同一个目录里，
    所以按"用户会怎么装它"的顺序找：
        一、~/.atrium/ 下面有东西 —— 说明配过（最常见）
        二、跟桥并排的目录（开发时）
        三、site-packages 里能 import
    """
    import os

    home = os.path.expanduser("~")
    cands = [
        os.path.join(home, ".atrium", "config.json"),
        os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "..", "atrium", "atrium", "__init__.py"),
    ]
    for p in cands:
        try:
            if os.path.exists(p):
                return os.path.abspath(p)
        except OSError:
            pass

    try:
        import atrium  # noqa: F401

        return getattr(atrium, "__file__", "") or "installed"
    except ImportError:
        return ""


def check_atrium() -> dict:
    """前台（接飞书 / 钉钉的那个）在不在、配全没有。

    ## ⚠️ 为什么依赖列表里要有它

        原来这里查的是 AstrBot —— 那是老版本的前台宿主，已经删了。
        **删了之后没补上新的**，于是依赖列表只剩一堆 pip 包，
        看着像"包都装了就能用"，其实少了接聊天平台的那一头。

        （2026-11-05 老爸指出：「依赖不应该加上我们的桥嘛，
         你给 AstrBot 删了不加上我们的桥」。）

    ## 这项查两件事

        一、atrium 装了没（有没有 ~/.atrium/config.json 或者能 import）
        二、配了平台没（飞书 app_id / 钉钉 client_id）

    两个都满足才算"能接聊天"。
    """
    import os

    where = _find_atrium()
    if not where:
        return {
            "key": "atrium",
            "label": "前台（atrium）",
            "installed": False,
            "version": "",
            "why": "接飞书 / 钉钉，派活都从它进来",
            "needed": True,
            "can_install": False,
            "note": "没装。装法见 atrium 的 README",
        }

    # 找到配置就读一下平台
    cfg = os.path.join(os.path.expanduser("~"), ".atrium", "config.json")
    chans: list[str] = []
    try:
        with open(cfg, encoding="utf-8") as f:
            ac = json.load(f)
        plats = ac.get("platforms") or {}
        if isinstance(plats, dict):
            for key, label in (("feishu", "飞书"), ("dingtalk", "钉钉")):
                p = plats.get(key)
                if not isinstance(p, dict):
                    continue
                if p.get("enabled") is False:
                    continue
                if key == "feishu" and p.get("app_id"):
                    chans.append(label)
                elif key == "dingtalk" and p.get("client_id"):
                    chans.append(label)
    except (OSError, json.JSONDecodeError):
        pass

    if chans:
        note = "已配：" + "、".join(chans)
        ok = True
    else:
        note = "装了，但还没配聊天平台"
        ok = False

    return {
        "key": "atrium",
        "label": "前台（atrium）",
        # ⚠️ installed 直接用 ok（配全没有），不是"文件在不在"。
        #
        #    界面的红绿点只看 installed 这一个字段。
        #    "装了但没配平台"跟"没装"一样干不了活 —— 就该是红的。
        #    原来想用 _ok_extra 另开一个字段，界面不认，等于没做。
        "installed": ok,
        "version": "",
        "why": "接飞书 / 钉钉，派活都从它进来",
        "needed": True,
        "can_install": False,
        "note": note,
    }


def check_all() -> list[dict]:
    """把清单里每一项都查一遍。

    ## 顺序是有讲究的

        一、Python        —— 地基
        二、桥（本项目）    —— 这是咱们自己的东西，不应该埋在一堆 pip 包里
        三、前台（atrium）  —— 接聊天平台的那一头
        四、pip 包        —— 干各种活用的

        原来只有 pip 包，看着像"包齐了就能用"，
        其实少了桥和前台 —— 那两个才是真正让东西跑起来的。

        （2026-11-05 老爸指出这一点。）
    """
    out = [check_python(), check_bridge(), check_atrium()]

    for d in DEPS:
        if d.get("is_python"):
            continue
        ok, ver = _module_installed(d["module"])
        out.append({
            "key": d["key"],
            "label": d["label"],
            "installed": ok,
            "version": ver,
            "why": d["why"],
            "needed": d["needed"],
            "pip": d["pip"],
            "can_install": True,
            "note": "",
        })

    # ⚠️ 依赖检查里**不列 AstrBot**。
    #
    #    AstrBot 的对接代码在仓库里留着（desktop/launcher.py 那一套），
    #    它**不是派活台的运行必需项** —— 派活台用 atrium 当前台。
    #
    #    要是列进依赖清单，界面上会永远挂一条
    #    「AstrBot 未找到 · [选择…]」，用户会以为少装了个必需的东西。
    #
    #    实测（2026-11-05）：老爸在系统页看到它，
    #    问"不是换成我们自己的桥了吗"。
    return out


# ── AstrBot 的路径探测没有列进依赖清单 ──
#
# 这里原来有四个函数：
#     _astrbot_paths_file   用户指定路径存哪
#     load_custom_paths     读用户指定的路径
#     save_custom_path      记住用户指定的路径
#     _check_astrbot        在几个常见位置找 AstrBot 装没装
#
# 它们的作用是"在依赖页上显示 AstrBot 装没装"。
# 派活台不用 AstrBot（前台在 atrium 项目里），
# 所以这条检查删了 —— 留着只会挂一条永远「未找到」的条目。
#
# **AstrBot 的对接实现本身还在**（desktop/launcher.py / channels.py / svc_flow.py），
# 那部分是另一回事，没动。
#
# （2026-11-05 删，老爸在系统页看见「AstrBot 未找到」问起来的。）


def missing(items: list[dict]) -> list[dict]:
    """缺哪些、而且能从桌面端装的。"""
    return [i for i in items if not i["installed"] and i.get("can_install")]


# ── 安装 ────────────────────────────────────────────────

class Installer:
    """装 pip 包，带进度回调。

    用 pip 的子进程，边读输出边解析进度。
    为什么不用 pip 的 API：
        pip 没有稳定的公开 API，直接 import pip 干活
        在官方眼里是"不支持的用法"，换版本就可能坏。
        当子进程调是它支持的用法。
    """

    def __init__(self, on_progress=None, on_done=None):
        self.on_progress = on_progress or (lambda msg, pct: None)
        self.on_done = on_done or (lambda ok, msg: None)
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def install(self, packages: list[str]) -> tuple[bool, str]:
        """装一批包。阻塞调用，进度通过回调出去。"""
        if not packages:
            return True, "没有要装的"

        cmd = [
            sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check",
            "--no-input",
            *packages,
        ]

        self.on_progress(f"开始安装：{', '.join(packages)}", 0)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=(
                    0x08000000 if sys.platform == "win32" else 0
                ),
            )
        except OSError as e:
            return False, f"起不来：{e}"

        lines: list[str] = []
        assert proc.stdout is not None
        for raw in proc.stdout:
            if self._cancelled:
                try:
                    proc.terminate()
                except OSError:
                    pass
                return False, "用户取消了"

            line = raw.rstrip("\r\n")
            if not line:
                continue
            lines.append(line)

            # 从 pip 的输出里挖进度
            pct = _parse_progress(line)
            if pct is not None:
                self.on_progress(line.strip(), pct)
            elif "Downloading" in line or "Collecting" in line:
                self.on_progress(line.strip(), -1)   # -1 表示"在动但不知道到哪"

            if len(lines) > 2000:
                lines = lines[-1000:]

        rc = proc.wait()

        if rc == 0:
            self.on_progress("装好了", 100)
            return True, "装好了"

        detail = "\n".join(lines[-6:])
        return False, f"没装上（退出码 {rc}）\n{detail}"


def _parse_progress(line: str) -> int | None:
    """从 pip 的下载行里挖出百分比。

    pip 的输出长这样：
        Downloading openpyxl-3.1.5-py3-none-any.whl (250 kB)
        ━━━━━━━━━━━━━━━━━━━━━━━━━━ 250.1/250.1 kB 1.2 MB/s eta 0:00:00
    """
    import re

    # 进度条那一行。
    #
    # pip 实际输出长这样（注意单位只写在末尾一次）：
    #     ━━━━━━ 250.1/250.1 kB 1.2 MB/s eta 0:00:00
    #
    # 我第一版的正则要求两边都带单位，于是这种写法一个都匹配不上。
    # 现在两种都认。
    unit = {"kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}

    # 形式一：250.1/250.1 kB
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*(kB|MB|GB)", line
    )
    if m:
        a, b, u = m.groups()
        try:
            total = float(b) * unit[u]
            if total > 0:
                return max(0, min(99, int(float(a) * unit[u] / total * 100)))
        except (ValueError, KeyError):
            pass

    # 形式二：250.1 kB/250.1 kB（两边都带单位）
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(kB|MB|GB)\s*/\s*(\d+(?:\.\d+)?)\s*(kB|MB|GB)",
        line,
    )
    if m:
        a, ua, b, ub = m.groups()
        try:
            total = float(b) * unit[ub]
            if total > 0:
                return max(0, min(99, int(float(a) * unit[ua] / total * 100)))
        except (ValueError, KeyError):
            pass

    # 有的版本直接带百分比
    m = re.search(r"(\d{1,3})%", line)
    if m:
        try:
            return max(0, min(99, int(m.group(1))))
        except ValueError:
            pass

    return None


def install_async(packages: list[str], on_progress, on_done) -> Installer:
    """在后台线程里装，不卡界面。"""
    inst = Installer(on_progress=on_progress, on_done=on_done)

    def _run():
        ok, msg = inst.install(packages)
        on_done(ok, msg)

    threading.Thread(target=_run, daemon=True, name="dep-install").start()
    return inst


# ── 环境信息（给界面显示）────────────────────────────────

def env_info() -> dict:
    """当前 Python 环境的关键信息。"""
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": sysconfig.get_platform(),
        "prefix": sys.prefix,
    }
