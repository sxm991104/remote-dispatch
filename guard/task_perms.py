"""任务端的权限级别。

老爸的设计
    默认**工作区权限**：只能在它自己的任务目录里干活。
    要碰工作区外面，得单独给那个窗口提权。

为什么默认要收
    任务端有完全权限是必须的（有些活就是要改 D:\\项目\\xxx）。
    但"默认全开"等于：一次成功的诱导 = 整个硬盘。

    "默认最小 + 按需提权"的差别在哪：
        攻击者要造成伤害，得先让用户同意提权。
        而提权是个明确动作，用户看得见。

三级
    workspace   只能在工作目录里（默认）
    home        能碰用户目录（文档、桌面这些）
    full        什么都能碰

    为什么不直接两档（工作区 / 全部）：
        中间这档很实用——"帮我整理桌面上的文件"不需要全盘权限，
        但工作区权限确实不够。
"""

from __future__ import annotations

import json
import os

# 各平台该把配置放哪儿，见 tools/userdirs.py
import tools.userdirs as _userdirs

LEVELS = ("workspace", "home", "full")

LEVEL_LABEL = {
    "workspace": "工作区",
    "home": "用户目录",
    "full": "完全",
}

LEVEL_HINT = {
    "workspace": "只能动它自己那个任务目录里的文件",
    "home": "能动你用户目录下的东西（文档、桌面、下载）",
    "full": "整个电脑都能动",
}


def _cfg_path() -> str:
    return _userdirs.config_file("task_perms.json")


def load() -> dict:
    """读每个窗口的权限设置。

    格式：
        {"别名的权限": "workspace", "另一个": "full"}

    没设置的按默认（workspace）。
    """
    p = _cfg_path()
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(d: dict) -> None:
    p = _cfg_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError:
        pass


def get_level(name: str) -> str:
    """这个窗口是什么权限。没设置过就是 workspace。"""
    lv = (load().get(name) or "").strip().lower()
    return lv if lv in LEVELS else "workspace"


def set_level(name: str, level: str) -> bool:
    """给一个窗口换权限。"""
    lv = (level or "").strip().lower()
    if lv not in LEVELS:
        return False
    d = load()
    if lv == "workspace":
        # 默认值不存，保持配置干净
        d.pop(name, None)
    else:
        d[name] = lv
    save(d)
    return True


def allowed_roots(level: str, workdir: str) -> list[str]:
    """这个权限级别下，能碰哪些根目录。

    ⚠️ 这里返回的是"允许的最上层"。
        具体判断交给 is_allowed()。
    """
    roots: list[str] = []

    if workdir:
        roots.append(workdir)

    if level in ("home", "full"):
        roots.append(os.path.expanduser("~"))

    if level == "full":
        # 全盘：用根路径覆盖一切
        if os.name == "nt":
            for drive in "CDEFGHIJ":
                p = f"{drive}:\\"
                if os.path.exists(p):
                    roots.append(p)
        else:
            roots.append("/")

    return roots


def _real(path: str) -> str:
    """规范化成真实路径：展开 ~、追符号链接、统一大小写。

    ⚠️ 为什么必须追链接：

        红队测试用 junction 逃出过一次。工作目录里建
        "link_out" 指向 C:\\Users\\me，然后写 "link_out/x.txt"。

        abspath 得到 <workdir>/link_out/x.txt —— 看着在工作区内。
        realpath 得到 C:\\Users\\me\\x.txt —— 实际在外面。

        只用 abspath 判前缀，工作区边界就废了。
    """
    try:
        return os.path.normcase(os.path.realpath(os.path.expanduser(path)))
    except (OSError, ValueError):
        return ""


def is_allowed(path: str, level: str, workdir: str) -> bool:
    """这个路径在当前权限下能不能碰。

    ⚠️ 两边都用 realpath：
        待判路径要追链接（不然顺着链接跑出去）；
        允许的根目录也要追（用户目录本身可能就是链接）。
    """
    if not path:
        return False

    target = _real(path)
    if not target:
        return False

    for root in allowed_roots(level, workdir):
        r = _real(root)
        if not r:
            continue

        # ⚠️ 根目录本身已经带分隔符了（"c:\\"），不能再补一个。
        #
        #    原来写成 r + os.sep，于是根目录变成 "c:\\"（两个反斜杠），
        #    而正常路径是 "c:\\windows\\..."（一个反斜杠），
        #    永远匹配不上 —— **完全权限那一档整个失效了**。
        #
        #    实测：设成 full 之后，连 C 盘都碰不到。
        if not r.endswith(os.sep):
            r = r + os.sep

        if target == r.rstrip(os.sep) or target.startswith(r):
            return True

    return False


# 需要提权时，错误文本里固定出现的标记。
#
# 前台不用理解整段话，只要看到这四个字，就照"要验证码 → 提权 → 续同一个窗口"
# 这套流程走。
#
# 为什么用方括号 + 固定字样：
#     前台模型对"带括号的固定短语"很敏感，
#     不容易被段落里的其他句子带偏。
#     和 HTTP 的 401、Redis 的 -MOVED 是一个思路：
#     机器读标记，人读文字。
NEED_ESCALATE = "【需要提权】"


def explain_denied(path: str, level: str) -> str:
    """被权限挡住时说什么。

    ⚠️ 第一句就带 NEED_ESCALATE 标记。
        前台的判断依据是这个标记，不是理解后面的段落。
    """
    label = LEVEL_LABEL.get(level, level)
    hint = LEVEL_HINT.get(level, "")
    nxt = next_level(level)
    nxt_label = LEVEL_LABEL.get(nxt, nxt) if nxt else ""

    if not nxt:
        # 已经是最高档，这个位置就是碰不了。
        # 这种不该说"需要提权"——提权也解决不了。
        return (
            f"这个位置碰不了（已经是最高权限了）：{path}\n"
            f"当前权限：{label}——{hint}\n"
            f"这多半是系统目录或者别的磁盘，不是提权能解决的。"
        )

    return (
        f"{NEED_ESCALATE} 这个位置不在当前权限能碰的范围里：{path}\n"
        f"当前权限：{label}——{hint}\n"
        f"提到「{nxt_label}」这一档就能碰。提权需要用户给验证码。"
    )


def next_level(level: str) -> str:
    """比当前档位高一档的那个。最高档返回空串。"""
    order = list(LEVELS)
    try:
        i = order.index(level)
    except ValueError:
        return ""
    return order[i + 1] if i + 1 < len(order) else ""


def is_need_escalate(text: str) -> bool:
    """这段错误是不是"需要提权"。

    前台、桥、桌面端都用这一个判据，不要各写各的。
    """
    return NEED_ESCALATE in (text or "")
