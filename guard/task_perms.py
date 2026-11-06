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

#: 撞到配置禁区时的标记。
#:
#: ⚠️ 必须跟 NEED_ESCALATE 分开。
#:
#:    实测（2026-11-06）：第一版用的是 NEED_ESCALATE，于是模型
#    看到"需要提权"就去走提权流程 —— 而这道闸根本不看权限档，
#    提权完回来了还是被拦，白烧一轮。
#:
#:    分开之后前台能认出"这个不是提权能解决的，得走接口"。
RESERVED_TAG = "【这是配置】"


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


# ══════════════════════════════════════════════════════
# 配置禁区
# ══════════════════════════════════════════════════════
#
# ## 规矩（用户 2026-11-06 定的）
#
#     任务端：**安全层面的配置，读写都不行。** 哪一档都不行。
#     前台：  可以改，但**只能走接口**，而且必须验证码。
#     桌面端：用户自己点，可以改（那是用户本人的手）。
#
# ## 为什么不能再靠"权限档"管这件事
#
#     原来这些配置全在用户目录里，而 home 档的意思是"用户目录都能碰"。
#     于是：
#
#         用户点「放宽到能碰桌面和下载」
#             ↓
#         实际交出去的还有：
#             · 权限表本身          → 能给自己改成 full
#             · guard.json          → 能改验证码邮箱、关掉验证码
#             · security_mode.json  → 能降到"出事可回退"
#             · engine.json         → 能改模型配置
#             · Startup 目录        → 下次开机执行任意东西
#
#     授权那句话里一个字都没提到这些。
#
#     （2026-11-06 被指出来：钥匙和锁放在同一个抽屉里，
#      而 home 档的权限正好是"这个抽屉随便开"。）
#
# ## 为什么不是"提权一下就能过"
#
#     别人家的文件是"用户的东西"，提权了能碰，那是用户的权利。
#     这些是**锁本身** —— 把它们放开，等于给出去一张万能钥匙：
#     任务端能改掉所有防火墙，然后想干什么干什么。
#
#     所以不是"要码"，是**不行**。连 full 档也不行。
#
# ## 前台怎么办
#
#     前台要调挡位、关配置 —— 走 HTTP 接口。
#     接口里做校验、要码、记日志。**读配置文件这条退路不留。**
#
#     （这也是"只能通过接口改"的意思：文件这条路上没有旁门。）


def _reserved_dirs() -> list[str]:
    """配置禁区：这些目录下的东西，任务端读写都不许。"""
    out: list[str] = []

    # 我们自己的配置目录（权限表、验证码、安全模式、引擎、审计员、token）
    try:
        from tools import userdirs as _ud

        out.append(_ud.config_dir())
    except Exception:  # noqa: BLE001
        pass

    home = os.path.expanduser("~")

    # DSH 自己的东西：API 密钥、会话、人设
    out.append(os.path.join(home, ".dsh"))

    # 前台（atrium）的平台凭据
    out.append(os.path.join(home, ".atrium"))

    # 开机自启 —— 写进去等于下次开机执行任意东西
    out.append(os.path.join(
        os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming"),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    ))

    # 计划任务目录 —— 同上，而且更高权限
    out.append(os.path.join(
        os.environ.get("SystemRoot") or r"C:\Windows",
        "System32", "Tasks",
    ))

    return [d for d in out if d]


def program_dirs() -> list[str]:
    """我们自己的程序目录。**代码就在这里面，改了等于改闸本身。**

    ⚠️ 为什么单独一个函数：
        工作区、配置区、程序目录是三种不同的东西，
        保护的方式也不一样（见对外的检查函数）。
        但"我们自己放的"这一层，它们是一回事。
    """
    out: list[str] = []

    # 桥/引擎所在的那个目录（guard/、engine/、server/、core/、desktop/ 的父目录）
    try:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if here:
            out.append(here)
    except Exception:  # noqa: BLE001
        pass

    return [d for d in out if d]


def workspaces_root() -> str:
    """所有任务工作区的根。

    ⚠️ 这个根本身是禁区（不能列、不能建、不能删），
       但它**底下每一个子目录只归那个任务自己**。
       所以这里返回的是"根"，具体放行在 is_allowed 里按 workdir 判。
    """
    try:
        from core import bridge as _b

        return _b.workspaces_dir()
    except Exception:  # noqa: BLE001
        return ""


#: 配置目录里**允许程序自己看**的东西不存在 —— 这里是全禁。
#: 之所以留这个常量，是为了以后要开个例外时有地方写。


def _real(p: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(p))
    except OSError:
        return os.path.normcase(os.path.abspath(p))


def is_reserved(path: str, workdir: str = "") -> bool:
    """这个路径是不是**我们自己放的**东西，或者别人的工作区。

    ## 拦三样（三档都拦，跟权限无关）

        一、我们自己的程序目录 —— 代码就是闸本身
        二、我们自己的配置目录 —— 锁本身
        三、别人的工作区 —— 一个任务的活是它自己的

    ## ⚠️ 为什么跟权限档完全无关

        传 level 进来就会有人想着"full 档放过一下"。
        而 full 的意思是"能，动你电脑里的东西"，
        不是"能改掉保护你的那些东西"。

    ## ⚠️ 工作区要传 workdir

        自己的工作区是能读写的（那是它干活的地方）。
        别人的工作区不能。

        不传 workdir 时按"任何工作区都算禁区"处理 ——
        调用方不知道自己在哪的话，从严。
    """
    if not path:
        return False

    target = _real(path)

    # ── 一、配置区 + 二、程序目录：绝对禁区 ──
    dirs = _reserved_dirs() + program_dirs()
    for d in dirs:
        r = _real(d)
        if not r:
            continue
        if not r.endswith(os.sep):
            r = r + os.sep
        if target == r.rstrip(os.sep) or target.startswith(r):
            return True

    # ── 三、工作区：根是禁区，别人的子目录也是 ──
    root = workspaces_root()
    if root:
        rr = _real(root)
        if not rr.endswith(os.sep):
            rr = rr + os.sep

        if target == rr.rstrip(os.sep):
            # 根本身 —— 不能列、不能建、不能删
            return True

        if target.startswith(rr):
            # 在某个任务的工作区里。是不是自己的？
            if not workdir:
                # 调用方没说自己在哪 —— 从严
                return True
            mine = _real(workdir)
            if not mine.endswith(os.sep):
                mine = mine + os.sep
            if not (target == mine.rstrip(os.sep) or target.startswith(mine)):
                # 是别人的工作区
                return True

    return False


def explain_reserved(path: str) -> str:
    """撞到配置禁区时给模型看的话。

    ⚠️ **不能用 NEED_ESCALATE 开头。**
       实测（2026-11-06）：第一版写的是「【需要提权】这个是配置」，
       模型看到"需要提权"就会去走提权流程 ——
       而提权在这儿根本没用（这道闸不看权限档）。
       结果是它绕一圈回来又被拦，白烧一轮。

       所以换一个明确的标记：RESERVED_TAG。
       前台那边认这个标记，知道"这不是提权能解决的，得走接口"。
    """
    return (
        f"{RESERVED_TAG} 这个不能碰：\n"
        f"  {path}\n"
        "\n"
        "跟权限档无关 —— 提权也不管用。\n"
        "可能的原因：\n"
        "  · 是我们自己的代码或配置 —— 改了等于把闸本身拆了\n"
        "  · 是别的任务的工作区 —— 那是它的活，不是你的\n"
        "\n"
        "要改配置的话：让用户自己在桌面端点，"
        "或者在聊天里调接口（会要验证码）。"
    )


# ══════════════════════════════════════════════════════
# 统一入口
# ══════════════════════════════════════════════════════

def check_access(path: str, level: str, workdir: str) -> tuple[bool, str]:
    """这个路径在当前条件下能不能碰。**所有检查都该走这里。**

    ## 为什么要有这一层

        原来有两套判断各跑各的：

            is_allowed()   看权限档（workspace / home / full）
            is_reserved()  看是不是我们自己的东西

        结果 full 档能碰到我们项目 —— is_reserved 判"禁区"，
        但没人去看它，因为调用方只调了 is_allowed。

        2026-11-06 实测确认过：full 档 is_allowed(程序目录)=True。

    ## 两件事，一个入口

        一、禁区：是的话直接不行（提权也不行）
        二、权限：在不在这一档能碰的范围里

    ## 三档的边界（用户 2026-11-06 定）

        workspace  只能看自己的工作区
        home       自己的工作区 + 自己的 home
        full       自己的工作区 + 整个电脑

        **共同点：看不到我们项目，看不到别人的工作区。**

    返回 (能不能, 不行的话为什么)
    """
    # 一、先看禁区（顺序有意义：先报"这是禁区"，
    #     因为它比"权限不够"更准确 —— 权限不够还能提权解决）
    if is_reserved(path, workdir):
        return False, explain_reserved(path)

    # 二、再看权限
    if not is_allowed(path, level, workdir):
        return False, explain_denied(path, level)

    return True, ""


