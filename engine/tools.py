"""给模型用的工具。

一共六个。不多不少：

    read_file     读文件（带行号，方便定位）
    write_file    整篇写
    edit_file     精确替换一段
    list_dir      列目录
    search_files  按内容搜
    run_command   跑一条命令

为什么只要这六个
    DSH 有几十个，但任务端真正用到的就是这几个。
    工具越多，模型越容易挑错；而且每个工具的描述都要占上下文。

    另外 `edit_file` 单独列出来是有原因的：
    让模型"整篇重写"来改一个字符，既慢又容易改坏别的地方。
    精确替换能让它只动该动的那一行。

安全上的取舍
    这里不做沙箱。任务端本来就是要在整台机器上干活的，
    加个只能写一个目录的限制，反而挡住正当用途（实测过：
    DSH 的 workspace-write 模式下，让它去 D 盘建个文件会直接报
    Access denied，用户以为程序坏了）。

    真正该做的是**让人能看见它干了什么**——所以每个工具都返回
    明确的结果，界面和日志里都能追。
"""

from __future__ import annotations

import fnmatch
import json
import os
import stat
import subprocess
import sys

# 单个文件读多少就算太大。超过这个数只给开头和结尾，
# 免得一次把上下文塞满。
MAX_READ_BYTES = 200_000
MAX_OUTPUT_CHARS = 20_000

# 当前任务的工作目录。
#
# ⚠️ 这一条是实测踩出来的：
#    模型说 write_file("test.txt")，用的是相对路径。
#    而工具按 Python 进程的 cwd 去解析——那是启动桥的目录，
#    不是任务的工作目录。结果文件写到了别处，
#    模型自己还"读"回来了（因为读也是同一个错目录），
#    一切看起来都对，只有用户在任务目录里找不到文件。
#
#    所以每个任务开始前必须把 _WORKDIR 设成它自己的工作目录，
#    所有相对路径都以那里为准。
_WORKDIR: str = os.getcwd()

# 当前任务的权限级别。
#
# 老爸的设计：**默认工作区权限。**
#
#     工作区    只能动它自己那个任务目录
#     用户目录  能动 ~ 下面的东西
#     完全      整个电脑
#
# 要提权得单独给那个窗口设——不是默认就全开。
_LEVEL: str = "workspace"


def set_workdir(path: str) -> None:
    """设定这一轮任务的工作目录。agent 开跑前调用。"""
    global _WORKDIR
    if path and os.path.isdir(path):
        _WORKDIR = path


def workdir() -> str:
    return _WORKDIR


def set_level(level: str) -> None:
    """设定这一轮任务的权限级别。agent 开跑前调用。"""
    global _LEVEL
    if level in ("workspace", "home", "full"):
        _LEVEL = level


def level() -> str:
    return _LEVEL


def _norm_for_check(path: str) -> str:
    """把路径规范化成"实际会落到哪"的样子，用来做权限判断。

    ⚠️ 这个函数是红队测试打出来的。

        原来 _resolve 里的权限判断用的是 os.path.abspath + normpath：
        **纯字符串运算，不解析符号链接。**

        于是这个攻击成立（两步，每步单独看都合法）：

            第 1 步  mklink /J link C:\\Users\\me
                    （在任务自己的工作目录里建一个 junction）

            第 2 步  write_file("link/任意路径")
                    → abspath 得到 <workdir>/link/任意路径
                    → 看着在工作区内 → 权限放行
                    → 实际写进了 C:\\Users\\me

        实测在 Windows 上复现成功：abspath 判为"在工作区内"，
        realpath 显示真实落点在工作区外，文件真的被写到外面了。

        **这击穿的正是权限模型本身**——不是绕过命令词表，
        是让"工作区"这个边界失去意义。

    做法：expanduser → realpath → normcase

    为什么 realpath 也拦得住"链接还不存在"的情况：
        它解析的是**已经存在的**每一段链接。链接不存在时，
        realpath 就等于普通规范化——建链接那一步由
        action_guard 的执行口清单拦。两层配合。
    """
    p = (path or "").strip().strip('"')
    if not p:
        return _WORKDIR
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(_WORKDIR, p)
    p = os.path.normcase(os.path.realpath(p))

    # ── program 3：硬链接 ──
    #
    # ⚠️ realpath 追不到硬链接，这一点必须单独处理。
    #
    #     实测：在任务目录里 `mklink /H innocent.txt 外面的secret.txt`
    #
    #         realpath(innocent.txt) = <workdir>/innocent.txt   ← 看着在里面
    #         内容 = TOP_SECRET                                  ← 读到了外面的
    #
    #     硬链接没有"目标"这个概念——它就是同一个文件的另一个名字。
    #     GetFinalPathNameByHandle 也追不到，返回的还是工作区那个名字。
    #
    #     唯一的判据是文件的链接数：
    #         普通文件   nlink = 1
    #         硬链接     nlink ≥ 2   ← 说明它在别处还有名字
    #
    #     {"nlink": 2} 意味着这个文件有另一个名字在外面，
    #     而那另一个名字我们控制不了（可能是 C:\Windows 下的东西）。
    #
    #     ⚠️ 误伤评估：实测两个项目目录共 75 个文件，nlink 全是 1。
    #        正常复制/编辑不会产生硬链接，只有明确的 mklink /H
    #        或者某些备份工具会。所以这个判据在实际使用中几乎不误伤。
    try:
        st = os.stat(p)

        # ⚠️ 只对**普通文件**做硬链接检查，不对目录做。
        #
        #    目录的 nlink 天然大于 1：
        #        目录的 nlink = 2 + 子目录个数
        #        （"." 算一个，".." 算一个，每多一个子目录 +1）
        #
        #    Linux 上 /tmp 这类目录 nlink 经常 ≥ 2，
        #    所以不加这个判断的话，**每一个目录都会被当成硬链接拒掉**。
        #
        #    实测：CI 上跑，list_dir(".") 直接抛
        #        PermissionError: 这个文件有另外的名字（硬链接）
        #        真实内容可能在允许范围外：/tmp/tmppzk_tdk6
        #
        #    而本机（Windows）测不出来——Windows 上目录的 nlink 是 0，
        #    永远不大于 1。这就是"本机全绿、CI 全红"的原因。
        #
        #    这是修 F-01 时自己引入的 bug：
        #    当时的判断是"nlink > 1 就拒"，没区分文件和目录。
        if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
            # 普通文件 nlink > 1 → 它在别处还有名字（硬链接）。
            # 那个名字我们控制不了，可能指向 C:\Windows 里的东西。
            # 所以不管它看着在哪，一律拒。
            raise PermissionError(
                f"[权限不够] 这个文件有另外的名字（硬链接），"
                f"真实内容可能在允许范围外：{p}\n"
                f"当前权限：{_LEVEL}。"
                f"要碰它的话，需要把这个窗口的权限提一档。"
            )
    except FileNotFoundError:
        pass    # 文件还不存在（新建），没有链接问题
    except PermissionError:
        raise
    except OSError:
        pass

    return p


def _resolve(path: str) -> str:
    """把模型给的路径解析成绝对路径。**顺便做权限检查。**

    相对路径以任务工作目录为准；绝对路径原样用。
    ~ 会展开成用户目录。

    ⚠️ 权限检查放在这里，是因为**所有文件工具都经过它**：

        read_file / write_file / edit_file / list_dir / search_files
        run_command 的 cwd

        一个地方把关，比每个工具各加一遍靠谱得多——
        漏一个就是漏洞。

    默认权限是"工作区"，也就是只能在任务自己的目录里动。
    要碰外面，得给那个窗口单独提权。

    ⚠️ 判断用 realpath（追符号链接），不是 abspath。

        红队测试用 junction 逃出去过一次：
        在工作目录里建个链接指向 C:\\Users\\me，
        abspath 看着还在工作区里，实际写到了外面。
    """
    p = (path or "").strip().strip('"')
    if not p:
        return _WORKDIR

    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(_WORKDIR, p)
    p = os.path.normpath(p)

    try:
        from guard import task_perms as _tp

        # 用真实路径判权限（追链接），不是用字符串形态
        if not _tp.is_allowed(_norm_for_check(p), _LEVEL, _WORKDIR):
            raise PermissionError(_tp.explain_denied(p, _LEVEL))
    except ImportError:
        pass

    return p


# 命令最短给多少秒。
#
# 为什么不直接用用户要的值：
#     起 cmd、写 .bat、chcp、call、读输出、等影子落盘……
#     这一套本身就要一两秒。
#     用户传 1 秒的话，连 echo hi 都会"超时"，等于没法用。
#
#     所以 5 秒是"最小可执行时间"，不是故意拖慢。
#     但返回的话里要把实际值说出来——
#     用户说 3 秒、实际限 5 秒，不说明白就是"说的时间不是做的时间"。
MIN_TIMEOUT = 5
MAX_TIMEOUT = 600


def _timeout_msg(command: str, want: int) -> str:
    """超时了怎么告诉用户。

    ⚠️ 要把"实际用的秒数"说出来。

        用户说 3 秒，程序按 5 秒跑的（最小可执行时间）。
        只回"命令超时（3 秒）"的话，用户以为程序乱来。
        说清楚是"最少 5 秒"，他才知道规矩是什么。
    """
    used = _effective_timeout(want)
    if used == int(want or 0):
        return f"命令超时（{used} 秒）被中止：{command}"
    return (
        f"命令超时（{used} 秒）被中止：{command}\n"
        f"（你要的是 {want} 秒，最短按 {MIN_TIMEOUT} 秒算——"
        f"起 cmd 和读输出本身要时间，给太少的话连 echo 都会超时。）"
    )

def _effective_timeout(want: int) -> int:
    """用户要的秒数 → 实际会用的秒数。"""
    return max(MIN_TIMEOUT, min(int(want or 0), MAX_TIMEOUT))

def _nowindow_flags() -> int:
    return 0x08000000 if sys.platform == "win32" else 0


def _build_args(command: str) -> list[str]:
    """把用户写的命令变成 subprocess 的参数列表。

    ⚠️ 这里踩过一个很隐蔽的坑，值得完整写下来。

        现象：`python -c "print('中文')"` 通过 run_command 跑，
              返回码 0，**但一个字都没输出**。

        排查：
            · 直接 subprocess.run([python, "-c", "print(...)"])  → 正常
            · 靠 subprocess.run(["cmd", "/c", 完整命令])        → 空输出

        根因：**cmd 会剥掉命令里的双引号。**

            cmd /c python -c "print('中文')"

            cmd 认为双引号只是"给参数分组用的"，会把它去掉。
            于是 python 收到的是：
                -c   print('中文')     ← 少了引号，而且被拆成两个参数
            它把这个当成语法错误或者空程序，安安静静退出，什么都不打印。

        第一次遇到是 `echo 中文` 乱码，修好解码之后又冒出这个。
        两个是完全不同的问题，长得很像：
            乱码    → 字节解错了
            空输出   → 参数被 cmd 吃掉了

    **修法：不用 cmd 当中间层。**

        命令里如果包含了双引号，就自己拆成参数列表——
        Windows 上引号本来就是用来分组的，我们自己拆比让 cmd 拆更准。

        没有双引号的简单命令，还是走 cmd /c
        （这样 echo、dir、type 这些内置命令仍然能用）。
    """
    if sys.platform != "win32":
        return ["/bin/sh", "-c", command]

    # 有双引号 → 自己拆，别让 cmd 动手
    if '"' in command:
        parts = _split_command(command)
        if parts:
            return parts

    # ⚠️ 不在这里拼 chcp。
    #
    #    试过 `["cmd", "/c", "chcp 65001 >nul & " + command]`，CI 上还是乱码。
    #
    #    原因是执行顺序：
    #        cmd 收到整个命令行 → **先按当前代码页解析中文** → 再执行 chcp
    #    所以 chcp 生效的时候，中文已经在参数解析阶段变成 ? 了。
    #
    #    换新控制台（CREATE_NO_WINDOW）时默认代码页是系统 ANSI 页，
    #    英文 Windows 是 cp1252，没有中文字符。
    #
    #    → 代码页必须在 cmd 启动之前就定下来。
    #      见 _run_on_windows()。
    return ["cmd", "/c", command]


def _split_command(command: str) -> list[str]:
    """按 cmd 的规则拆参数：双引号包起来的部分算一个参数。

    比如：
        python -c "print('中文')"
        → ["python", "-c", "print('中文')"]

    拆不出来（引号不配对）就返回空列表，让调用方回退到 cmd。
    """
    parts: list[str] = []
    cur: list[str] = []
    in_quote = False

    for ch in command:
        if ch == '"':
            in_quote = not in_quote
            continue
        if ch in (" ", "\t") and not in_quote:
            if cur:
                parts.append("".join(cur))
                cur = []
            continue
        cur.append(ch)

    if in_quote:
        # 引号没配平，不拆
        return []

    if cur:
        parts.append("".join(cur))

    if not parts:
        return []

    # 第一个词如果是已知的内置命令（echo/dir/cd…），还是得交给 cmd
    builtin = {"echo", "dir", "type", "copy", "del", "cd", "chdir",
               "set", "cls", "ver", "vol", "md", "mkdir", "rd", "rmdir",
               "ren", "rename", "move", "tasklist", "taskkill", "where"}
    if parts[0].lower() in builtin:
        return []

    return parts


def _decode_output(raw) -> str:
    # ⚠️ 沙箱那条路返回的是 str（从文件读出来的），
    #    普通 subprocess 返回的是 bytes。
    #    这里两种都得吃。
    if isinstance(raw, str):
        return raw
    # 把子进程的输出字节变成文字。
#
    # ⚠️ 这里踩过一个很隐蔽的坑，值得写清楚。
#
        # 现象：`echo 中文测试` 出来是 `���Ĳ���`。
#
        # 排查过程：
            # · 直接跑 subprocess.run(["cmd","/c","echo 中文"]) → 正确
            # · 通过 run_command 跑 → 乱码
            # · 对比参数，差在 creationflags
            # · 带 CREATE_NO_WINDOW → 子进程输出 GBK 字节
              # 不带                 → 输出 UTF-8 字节
#
        # 为什么加了 CREATE_NO_WINDOW 就变成 GBK：
            # cmd.exe 的输出编码跟着控制台的代码页走。
            # 带 CREATE_NO_WINDOW 时不继承父进程的控制台，
            # 自己起一个默认代码页 936（简体中文 GBK）。
            # 不带时继承了父进程的（我们设了 UTF-8）。
#
        # 试过但不可靠的做法：
            # · 统一按 UTF-8 解 → 带 creationflags 时乱码
            # · 统一按 GBK 解   → 不带时乱码
            # · 强制给子进程设 PYTHONIOENCODING → 只管 Python 子进程，
              # cmd.exe 不吃这个变量
#
        # 最终做法：**先试 UTF-8，失败再用 GBK**。
#
            # UTF-8 的字节序列有很强的校验性——中文乱码成 GBK
            # 很难恰好是合法 UTF-8。所以"严格试 UTF-8"是可靠的判据。
#
            # 实测两种都能正确解：
                # UTF-8 字节 → 严格解码成功 → 对
                # GBK 字节   → 严格解码抛异常 → 转 GBK → 对
    #
    if not raw:
        return ""

    # 先严格试 UTF-8
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # 再试 GBK（简体中文 Windows 的默认代码页）
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        pass

    # 都不行：用 UTF-8 + replace，至少不崩
    return raw.decode("utf-8", "replace")


# ----------------------------------------------------------------------
# 工具的实现
# ----------------------------------------------------------------------

def read_file(path: str, offset: int = 1, limit: int = 0) -> str:
    p = _resolve(path)
    if not os.path.exists(p):
        return f"文件不存在：{p}"
    if os.path.isdir(p):
        return f"这是个目录，不是文件：{p}"

    size = os.path.getsize(p)
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return f"读不了：{e}"

    total = len(lines)
    start = max(0, offset - 1)
    end = total if limit <= 0 else min(total, start + limit)
    chunk = lines[start:end]

    body = "".join(f"{i + start + 1:>6}\t{ln}" for i, ln in enumerate(chunk))
    if size > MAX_READ_BYTES:
        body = body[:MAX_READ_BYTES] + "\n…（文件太大，只读了前面一段）"

    head = f"{p}（共 {total} 行，这里是第 {start + 1}~{end} 行）\n"
    return head + body


def write_file(path: str, content: str) -> str:
    p = _resolve(path)
    parent = os.path.dirname(os.path.abspath(p))
    try:
        os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    except OSError as e:
        return f"写不了：{e}"
    return f"已写入 {p}（{len(content)} 字符）"


def edit_file(path: str, old: str, new: str, replace_all: bool = False) -> str:
    p = _resolve(path)
    if not os.path.exists(p):
        return f"文件不存在：{p}"
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return f"读不了：{e}"

    n = text.count(old)
    if n == 0:
        return (
            "没找到要替换的内容。\n"
            "注意 old 必须和文件里的原文完全一致（包括空格和缩进）。\n"
            "建议先用 read_file 看一眼要改的那几行。"
        )
    if n > 1 and not replace_all:
        return (
            f"这段内容在文件里出现了 {n} 次，替换哪一个不明确。\n"
            "把 old 写长一点、带上前后文，让它唯一；"
            "或者明确说 replace_all=true。"
        )

    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(updated)
    except OSError as e:
        return f"写不了：{e}"

    hit = n if replace_all else 1
    return f"已改 {p}（替换了 {hit} 处）"


def list_dir(path: str) -> str:
    p = _resolve(path)
    if not os.path.isdir(p):
        return f"目录不存在：{p}"
    try:
        names = sorted(os.listdir(p))
    except OSError as e:
        return f"看不了：{e}"

    out = []
    for n in names[:400]:
        full = os.path.join(p, n)
        if os.path.isdir(full):
            out.append(f"{n}/")
        else:
            try:
                out.append(f"{n}  ({os.path.getsize(full)} B)")
            except OSError:
                out.append(n)
    more = f"\n…还有 {len(names) - 400} 项" if len(names) > 400 else ""
    return f"{p}/\n" + "\n".join(out) + more


def search_files(pattern: str, path: str = ".", glob_filter: str = "") -> str:
    """按内容搜。用 Python 自己走目录，不依赖 grep（有些机器没有）。"""
    root = _resolve(path)
    if not os.path.isdir(root):
        return f"目录不存在：{root}"

    hits = []
    scanned = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            # 跳过明显的噪音目录
            dirnames[:] = [
                d for d in dirnames
                if d not in (".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build")
            ]
            for fn in filenames:
                if glob_filter and not fnmatch.fnmatch(fn, glob_filter):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(full) > 2_000_000:
                        continue
                    with open(full, encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if pattern in line:
                                hits.append(f"{full}:{i}: {line.strip()[:200]}")
                                if len(hits) >= 100:
                                    raise StopIteration
                except (OSError, UnicodeDecodeError):
                    continue
                scanned += 1
                if scanned > 5000:
                    raise StopIteration
    except StopIteration:
        pass

    if not hits:
        return f"没找到包含「{pattern}」的地方（看了 {scanned} 个文件）"
    return "\n".join(hits) + f"\n（共 {len(hits)} 处）"


def _usable_temp() -> str:
    """找一个能用的临时目录。

    ⚠️ 为什么不能直接用 TEMP：

        实测（GitHub 的 Windows runner）：
        runner 用 bash（MINGW64）跑，$TEMP 被 MSYS 转成了 /tmp。
        那是 POSIX 路径，Windows 上不存在。

        于是 tempfile.mkstemp() 失败 → 写不出那个 .bat →
        cmd 起不来 → 所有跑命令的测试全挂，
        而报出来的是退出码 4294967295（= -1），用户看不懂。

        同一份代码在 Ubuntu 上全过，在本机（TEMP 是 Windows 路径）也全过。

    所以：先验证 TEMP 能不能用（建一个文件、删掉），
          不行就退到 USERPROFILE\\AppData\\Local\\Temp，
          再不行退到系统盘根下的一个子目录。
    """
    import tempfile as _tf

    cands = []
    t = os.environ.get("TEMP") or ""
    if t:
        cands.append(t)
    t2 = os.environ.get("TMP") or ""
    if t2:
        cands.append(t2)
    # 正规的 Windows 用户临时目录
    up = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if up:
        cands.append(os.path.join(up, "AppData", "Local", "Temp"))
    # 最后兜底：系统盘
    sysdrive = os.environ.get("SystemDrive") or "C:"
    cands.append(os.path.join(sysdrive + os.sep, "Windows", "Temp"))

    for c in cands:
        if not c:
            continue
        # POSIX 路径在 Windows 上不可能是有效目录
        if os.name == "nt" and c.startswith("/"):
            continue
        try:
            if not os.path.isdir(c):
                os.makedirs(c, exist_ok=True)
            # 真的能建能删吗
            fd, probe = _tf.mkstemp(dir=c, prefix=".dsh_probe_")
            os.close(fd)
            os.remove(probe)
            return c
        except OSError:
            continue

    # 全都不行：交给系统自己挑（可能也失败，但至少不是我们硬塞的）
    return _tf.gettempdir()

# ══════════════════════════════════════════════════════
# 进程树超时控制
# ══════════════════════════════════════════════════════
#
# ⚠️ 为什么不能用 subprocess.run(timeout=)：
#
#     它在 Windows 上只杀**直接子进程**（cmd.exe），
#     然后 join 两个读管道的线程 —— 而那两个管道的句柄
#     被孙进程继承着，孙进程不退出，join 就永远等。
#
#     实测：命令是 python child.py（里面 sleep 120），
#          timeout=5 → 实际 120.3 秒才返回。
#          也就是说超时形同虚设。
#
#     同一个坑踩过两次（还有一次是 timeout=3 跑了 34 秒），
#     所以这次做成显式的、带注释的、有测试的。
#
# 做法：
#     Windows   Job Object + KILL_ON_JOB_CLOSE
#               把 cmd 挂进 Job，它起的子进程自动也进 Job；
#               超时调 TerminateJobObject，整棵树一起死。
#     POSIX     start_new_session + killpg


class _JobTree:
    """把子进程挂进一个 Job（Windows）/ 进程组（POSIX），能整棵杀掉。"""

    def __init__(self):
        self._job = None
        self._proc = None
        self.is_windows = sys.platform == "win32"

    # ── Windows ──
    def _win_make_job(self):
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        info.BasicLimitInformation.LimitFlags = 0x2000
        ok = kernel32.SetInformationJobObject(
            job,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            kernel32.CloseHandle(job)
            return None
        return job

    def _win_assign(self, job, pid: int) -> bool:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # PROCESS_ALL_ACCESS
        handle = kernel32.OpenProcess(0x1F0FFF, False, pid)
        if not handle:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(job, handle))
        finally:
            kernel32.CloseHandle(handle)

    # ── 对外 ──
    def spawn(self, argv, **kw):
        import subprocess

        if self.is_windows:
            job = self._win_make_job()
            # 先建进程再挂 Job —— 中间有个极短的窗口期，
            # 但 CreateProcess 那一刻还没跑起来，够用。
            p = subprocess.Popen(argv, **kw)
            if job and not self._win_assign(job, p.pid):
                import ctypes

                ctypes.WinDLL("kernel32").CloseHandle(job)
                job = None
            self._job = job
            self._proc = p
            return p

        kw.setdefault("start_new_session", True)
        p = subprocess.Popen(argv, **kw)
        self._proc = p
        return p

    def kill_tree(self) -> None:
        """把整棵树杀掉。已经有进程退了也无所谓。"""
        p = self._proc
        if p is None:
            return
        if self.is_windows and self._job:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # TerminateJobObject 会把 Job 里所有进程都终止
            if not kernel32.TerminateJobObject(self._job, 1):
                # 退路：至少杀掉直接子进程
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
            return

        # POSIX：整组杀
        try:
            import os as _os
            import signal as _sig

            _os.killpg(_os.getpgid(p.pid), _sig.SIGKILL)
        except Exception:  # noqa: BLE001
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        """收尸。Job 句柄一关，KILL_ON_JOB_CLOSE 会兜底杀掉漏网的。"""
        if self.is_windows and self._job:
            try:
                import ctypes

                ctypes.WinDLL("kernel32").CloseHandle(self._job)
            except Exception:  # noqa: BLE001
                pass
            self._job = None

def _run_on_windows(command: str, work_dir: str, env: dict, timeout: int):
    """Windows 上跑一条命令，用"写文件再执行"的方式保住中文。

    ⚠️ 为什么不用命令行参数传命令：

        cmd.exe 拿到命令行之后，会按**当前代码页**把它转成 ANSI
        再解析。英文 Windows 的代码页是 cp1252——里面没有中文字符，
        中文在这一步就变成 ? 了。

        而且这个转换发生在 chcp 生效**之前**，所以
        "chcp 65001 & echo 中文" 也救不了：
        chcp 换的是之后的输出编码，中文参数早就坏了。

        在中文 Windows（代码页 936）上测不出来，因为 GBK 有中文。

    做法：

        1. 把命令写成 UTF-8 的 .bat 文件
        2. 命令行只传给 cmd 三样东西，全是 ASCII：
              chcp 65001 >nul   切代码页
              call 文件名        执行
        3. 删掉临时文件

    返回形状和 subprocess.CompletedProcess 一样，
    调用方不用分平台处理。超时返回 None。
    """
    import tempfile

    class _Result:
        __slots__ = ("returncode", "stdout", "stderr", "_start_failed", "_err")

        def __init__(self, returncode=0, stdout=b"", stderr=b""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr
            self._start_failed = False
            self._err = ""

    try:
        # ⚠️ 不用 tempfile.mkstemp() 的默认目录——
        #    它看 TEMP，而 TEMP 可能是 /tmp 这种 POSIX 路径。
        #    详见 _usable_temp 的说明。
        _tdir = _usable_temp()
        fd, bat = tempfile.mkstemp(
            prefix="dshcmd_", suffix=".bat", dir=_tdir
        )
        os.close(fd)
    except OSError as e:
        r = _Result()
        r._start_failed = True
        r._err = str(e)
        return r

    try:
        with open(bat, "w", encoding="utf-8", newline="\r\n") as f:
            f.write("@echo off\r\n")
            f.write(command)
            f.write("\r\n")

        _to = _effective_timeout(timeout)
        _tree = _JobTree()
        try:
            _p = _tree.spawn(
                ["cmd", "/c", "chcp 65001 >nul & call " + bat],
                cwd=work_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=_nowindow_flags(),
            )
        except OSError as e:
            _tree.close()
            r = _Result()
            r._start_failed = True
            r._err = str(e)
            return r

        try:
            # ⚠️ 自己等，不用 p.communicate(timeout=)：
            #    那个超时只杀直接子进程，然后 join 读管道的线程，
            #    被孙进程卡住（实测 5 秒变 120 秒）。
            #    这里到点直接 kill_tree()，整棵树一起死。
            _out, _err2 = _p.communicate(timeout=_to)
            # 返回跟 subprocess.run 一样的形状
            # （.returncode / .stdout / .stderr 都是 bytes）
            return _Result(_p.returncode, _out or b"", _err2 or b"")
        except subprocess.TimeoutExpired:
            _tree.kill_tree()
            try:
                _p.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            return None
        finally:
            _tree.close()
    finally:
        try:
            os.remove(bat)
        except OSError:
            pass


# ══════════════════════════════════════════════════════
# 影子执行（Sandboxie）
# ══════════════════════════════════════════════════════
#
# 为什么要有这一段
#
#     守卫在"写脚本再执行"这条路上一直是空的：
#     命令字符串里没有危险词，脚本内容又千变万化，
#     词表追不完、内容扫描也追不完。
#
#     但"删除"这个动作躲不掉——它一定落在文件系统上。
#     所以不猜它要干什么，看它真干了什么。
#
#     沙箱把顺序倒过来：删除先在影子层发生，真文件不动；
#     跑完看影子里记了哪些删除；有就扣着，等用户点头。
#
# ⚠️ 没装 Sandboxie 就退回原路，一次不差。
#    但结果里要写明"这次没有影子保护"，不能让调用方以为有。

_SANDBOX_CACHE = None

# 这次任务里被砂箱扣下来的删除。用户确认前一直挂着。
#
# 为什么是模块级：一个任务可能跑好几条命令，
# 每条都可能删东西。攒着一起问，别一条一问把人弹麻。
_PENDING_DELETES: list[str] = []


def _sandbox_state() -> dict:
    """影子执行能不能用。探测一次缓存住。

    ⚠️ 两个平台两套实现，语义对齐：

        Windows   Sandboxie 的延迟删除 + 影子文件
        Linux     unshare --user --mount --net + overlayfs

    实测（2026-11-03）：
        Linux 上沙箱里改 / 删 / 建文件，真机一个字节都没动，
        而且 curl 出不去（--net）。
    """
    global _SANDBOX_CACHE
    if _SANDBOX_CACHE is not None:
        return _SANDBOX_CACHE

    d: dict = {"ok": False, "why": "未探测", "kind": ""}

    # ⚠️ 先看用户选的安全模式要不要影子执行。
    #
    #    模式一（严格）  要
    #    模式二（回滚）  **Windows 不要 / Linux 要**
    #                   Windows 的回退靠系统卷影副本，跟影子无关；
    #                   Linux 的版本历史**就是**影子层给的。
    #    模式三（全开）  要
    try:
        from guard import security_mode as _sm

        if not _sm.wants("shadow"):
            _SANDBOX_CACHE = {
                "ok": False,
                "why": f"当前是「{_sm.MODE_LABEL.get(_sm.get(), '?')}」模式，"
                       "这一档不用影子执行",
                "kind": "",
                "by_mode": True,
            }
            return _SANDBOX_CACHE
    except Exception:  # noqa: BLE001
        pass

    try:
        if sys.platform == "win32":
            from guard import sandbox as _sb

            if not _sb.available():
                d = {"ok": False, "why": "没装 Sandboxie", "kind": "win"}
            elif not _sb.box_exists():
                d = {"ok": False, "why": "沙箱还没建", "kind": "win"}
            else:
                d = {"ok": True, "why": "", "kind": "win"}
        else:
            from guard import sandbox_linux as _sl

            if _sl.available():
                d = {"ok": True, "why": "", "kind": "linux"}
            else:
                d = {
                    "ok": False,
                    "why": f"Linux 影子层不可用：{_sl.describe()}",
                    "kind": "linux",
                }
    except ImportError as e:
        d = {"ok": False, "why": f"沙箱模块不在：{e}", "kind": ""}
    except Exception as e:  # noqa: BLE001
        d = {"ok": False, "why": f"{type(e).__name__}: {e}", "kind": ""}

    _SANDBOX_CACHE = d
    return d


def reset_sandbox_cache() -> None:
    """用户刚装完沙箱时调一下，让下次重新探测。"""
    global _SANDBOX_CACHE
    _SANDBOX_CACHE = None


def sandbox_status() -> str:
    """一句话说清当前影子保护的状态。给界面和日志用。"""
    try:
        from guard import sandbox as _sb

        return _sb.describe()
    except ImportError:
        return "影子执行：模块不在"
    except Exception as e:  # noqa: BLE001
        return f"影子执行：探测失败（{type(e).__name__}）"


# 被扣下的删除，落盘存一份。
#
# 为什么不能只放内存：
#     它生成时在 worker 进程里，而"用户给了验证码、该真删了"
#     发生在另一个进程（桥接口或前台）。
#     worker 跑完就退，内存里的记录跟着没了 ——
#     于是 commit_pending_deletes 调了也没东西可删。
#
#     实测：全仓 grep commit_pending_deletes，除定义外零调用。
#     不是忘了调，是调了也空。
def _pending_path() -> str:
    """扣下的删除存哪。跟 bridge 的 state 目录放一起。"""
    import os as _os

    base = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    d = _os.path.join(base, "state")
    try:
        _os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return _os.path.join(d, "pending_deletes.json")


def _load_pending() -> dict:
    import json as _json

    p = _pending_path()
    try:
        import os as _os

        if _os.path.exists(p):
            d = _json.loads(open(p, encoding="utf-8").read())
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def _save_pending(d: dict) -> None:
    import json as _json
    import os as _os
    import tempfile as _tf

    p = _pending_path()
    try:
        fd, tmp = _tf.mkstemp(dir=_os.path.dirname(p), suffix=".tmp")
        _os.close(fd)
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(d, f, ensure_ascii=False)
        _os.replace(tmp, p)
    except OSError:
        try:
            _os.remove(tmp)  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            pass


def _remember_pending(files: list[str], task_id: str = "") -> None:
    """把这次扣下的删除记到盘上。"""
    if not files:
        return
    d = _load_pending()
    key = task_id or "_"
    cur = d.get(key) or {}
    got = list(cur.get("files") or [])
    for f in files:
        if f not in got:
            got.append(f)
    import datetime as _dt

    d[key] = {
        "files": got,
        "at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _save_pending(d)


def pending_deletes(task_id: str = "") -> list[str]:
    """还没确认的删除。

    ⚠️ 现在从盘上读，不再只看内存。
        内存那份是给"同一个进程里连着跑几条命令"用的，
        盘上那份是给"用户给了码、另一个进程要真删"用的。

    ⚠️ 这个函数原来定义了**两次** —— 下面还有个
       `pending_deletes()` 不收参数，把这份覆盖了。
       结果 commit_pending_deletes 传 task_id 进来直接 TypeError，
       解锁之后"待确认的删除"永远做不掉。

       实测（2026-11-04）：解锁时提示
       "（待确认的删除没做完：TypeError）"。

       跟之前 versions_of 重复定义是同一类错。
    """
    d = _load_pending()
    if task_id:
        return list((d.get(task_id) or {}).get("files") or [])
    out: list[str] = []
    for v in d.values():
        for f in (v or {}).get("files") or []:
            if f not in out:
                out.append(f)
    return out


def commit_pending_deletes(task_id: str = "") -> list:
    """把扣着的删除真执行掉。验证码通过之后调。

    ⚠️ 现在从**盘上**读，不只看内存。

        原因：这些删除是 worker 进程扣下的，
        而验证码通过发生在另一个进程。
        原来只读模块级 _PENDING_DELETES —— 那个进程早退了，
        所以 commit 永远删不到东西。

    task_id 传空 = 把所有任务扣着的都删。

    返回 sandbox.commit_deferred 的结果（成功/失败的清单）。
    """
    global _PENDING_DELETES

    from_disk = pending_deletes(task_id)
    targets: list[str] = []
    for f in list(_PENDING_DELETES) + from_disk:
        if f not in targets:
            targets.append(f)

    if not targets:
        return []

    # ⚠️ 真删之前再过一次权限。
    #
    #    用户给验证码是"同意删这件事"，
    #    不代表"忽略权限档"。
    #    万一有人把 task_id 对错了，也不至于删到不该删的地方。
    allowed: list[str] = []
    denied: list[str] = []
    try:
        from guard import task_perms as _tp

        wd = workdir()
        lv = _LEVEL
        for f in targets:
            try:
                if _tp.is_allowed(f, lv, wd):
                    allowed.append(f)
                else:
                    denied.append(f)
            except Exception:  # noqa: BLE001
                denied.append(f)      # 判不出来不算允许
    except Exception:  # noqa: BLE001
        # 连权限模块都拿不到 —— 一条都不删
        return []

    res: list = []
    if allowed:
        try:
            from guard import sandbox as _sb

            res = _sb.commit_deferred(allowed)
        except Exception:  # noqa: BLE001
            res = []

    # 从盘上删掉已经处理完的
    d = _load_pending()
    if task_id:
        d.pop(task_id, None)
    else:
        d.clear()
    _save_pending(d)
    _PENDING_DELETES = []

    return res


def discard_pending_deletes() -> None:
    """把扣着的删除丢掉（用户不认这笔）。"""
    global _PENDING_DELETES
    _PENDING_DELETES = []


def _net_fallback_allowed() -> tuple[bool, str]:
    """沙箱不可用时，能不能退回普通执行（有网）。

    返回 (能不能, 为什么)。

    ⚠️ 两种"不可用"要分开，不能一刀切：

        一、没装 Sandboxie
            正常配置。允许普通执行，但要告诉用户网络那层没了。

        二、装了但起不来
            可疑。除非用户填码授权联网，否则**拒绝**。

            "出事了反而更宽松"是最糟的设计：
            装好的沙箱突然不起作用，恰恰是最该警惕的时候。
    """
    from guard import netguard as _ng

    # 先看装的机器上断没断网 —— 决定"普通执行"到底有没有网
    try:
        st = _ng.status()
        box_ok = bool(st.get("box_block"))
    except Exception:  # noqa: BLE001
        box_ok = False

    from guard import sandbox as _sb

    if not _sb.available():
        # 没装。正常配置，允许。
        return True, "没装 Sandboxie，网络那层不存在（这不是故障）"

    # 装了。看有没有联网授权
    from guard import netpolicy as _np

    ok, left = _np.granted()
    if ok:
        return True, f"用户授权联网，还剩 {left // 60} 分钟"

    return False, (
        "沙箱装好了但起不来 —— 这时候不放行普通执行。"
        "如果你确实需要联网干活，填验证码授权（默认 30 分钟）。"
    )


def _run_in_sandbox_if_possible(command: str, work_dir: str, env: dict,
                                timeout: int):
    """在影子里跑。不可用返回 None。

    ⚠️ 两个平台两套实现，语义对齐：

        Windows   Sandboxie。跑完要 snapshot 对比、apply_after_run 回写、清影子。
        Linux     unshare + overlayfs。跑完直接看 upper 层就知道改了什么，
                  不用快照也不用回写。

    ⚠️ 这里只管"跑"和"记下删除"，不管"问用户"。
        问用户是异步的（发邮件、等人回），
        而 run_command 是同步阻塞的——模型在等结果。
    """
    st = _sandbox_state()
    if not st["ok"]:
        return None

    kind = st.get("kind") or ""

    # ── Linux：unshare + overlayfs ──
    if kind == "linux":
        try:
            from guard import sandbox_linux as _sl
        except ImportError:
            return None

        try:
            p = _sl.run_in_box(
                command,
                cwd=work_dir,
                env=env,
                timeout=_effective_timeout(timeout),
            )
        except subprocess.TimeoutExpired:
            raise
        except Exception:  # noqa: BLE001
            return None

        if p is None:
            return None

        # 改了什么，直接从 upper 层读
        try:
            global _PENDING_DELETES
            ch = _sl.classify_changes(work_dir)

            # 删除先扣着，等验证码 / 审核
            for real in ch.get("deletes") or []:
                if real not in _PENDING_DELETES:
                    _PENDING_DELETES.append(real)

            # 写入和修改：Linux 这边**由 apply_after_run 统一回写**，
            # 但那个函数在 run_command 里 —— 这里只把结果挂到 p 上，
            # 让上层知道 Linux 有改动要处理。
            try:
                p._dsh_linux_changes = ch
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

        return p

    # ── Windows：Sandboxie ──
    try:
        from guard import sandbox as _sb
    except ImportError:
        return None

    try:
        base = _sb.snapshot()
    except Exception:  # noqa: BLE001
        base = set()

    try:
        p = _sb.run_in_box(
            command,
            cwd=work_dir,
            env=env,
            timeout=_effective_timeout(timeout),
            capture=True,
        )
    except subprocess.TimeoutExpired:
        # ⚠️ 超时 ≠ 沙箱不可用。
        #    原来这里 `except Exception: return None`，
        #    调用方看到 None 就用普通方式再跑一遍 —— 时间翻倍。
        raise
    except Exception:  # noqa: BLE001
        return None

    if p is None:
        return None

    # 跑完了，分两件事处理：
    #   一、写入/修改 → 立刻回写到真机（用户无感）
    #   二、删除      → 扣着，等验证码
    try:
        import time as _t

        _t.sleep(0.6)      # 影子落盘有一点点延迟
        res = _sb.apply_after_run(baseline=base)
        got = res.get("deletes") or []
        _sb.clean_written_shadows(baseline=base)
    except Exception:  # noqa: BLE001
        got = []

    if got:
        for g in got:
            if g not in _PENDING_DELETES:
                _PENDING_DELETES.append(g)

        try:
            from guard import guard_gate as _gate

            for g in got:
                _gate.intercept(g, f"沙箱里发现删除：{g}")
        except Exception:  # noqa: BLE001
            pass

    return p
def _linux_apply_changes(work_dir: str, r):
    """把 Linux 影子层里的改动落地。

    ⚠️ 顺序很重要：

        一、先分类（改了什么）
        二、**审核**（guard/auditor.py）
        三、审核通过才回写
        四、清掉影子层

    反过来的话文件已经落地了才问"这改得对不对" ——
    那时候原文已经被覆盖，问也没用了。

    返回 r（CompletedProcess），只可能在需要时改它的 stdout。
    """
    from guard import sandbox_linux as _sl

    try:
        ch = _sl.classify_changes(work_dir)
    except Exception:  # noqa: BLE001
        try:
            _sl.cleanup(work_dir)
        except Exception:  # noqa: BLE001
            pass
        return r

    has_any = bool(ch.get("deletes") or ch.get("writes") or ch.get("modifies"))
    if not has_any:
        try:
            _sl.cleanup(work_dir)
        except Exception:  # noqa: BLE001
            pass
        return r

    # ── 审核 ──
    # 审核不过 → 一个字都不落
    try:
        from guard import auditor as _aud

        audit = _aud.review(ch)
    except Exception as e:  # noqa: BLE001
        audit = {"verdict": "skip", "reasons": [f"审核员异常：{e}"]}

    if audit and audit.get("verdict") in ("block", "asking", "locked"):
        # 扣住。影子层先留着 —— 等用户填了码再说。
        why = "；".join(audit.get("reasons") or [])
        try:
            extra = (
                f"\n[审核] 这次改动被扣住了：{why}\n"
                "[审核] 文件**没有**落到真机上。\n"
            )
            if r is not None and getattr(r, "stdout", None) is not None:
                r.stdout = (r.stdout or b"") + extra.encode("utf-8")
        except Exception:  # noqa: BLE001
            pass
        return r

    # ── 回写：新建 / 修改 ──
    written = 0
    failed = 0

    for shadow, real in (ch.get("writes") or []) + (ch.get("modifies") or []):
        try:
            os.makedirs(os.path.dirname(real), exist_ok=True)
            # ⚠️ 用 copy2 保留权限位。
            #    shutil.copy 会按默认 umask 建文件 ——
            #    脚本丢了执行位就跑不起来了。
            import shutil

            shutil.copy2(shadow, real)
            written += 1
        except (OSError, shutil.Error):
            failed += 1

    # ── 删除：扣着等验证码 ──
    # ⚠️ 这里**不真删**。真删要用户确认（guard_gate 那套）。
    pend = ch.get("deletes") or []
    if pend:
        global _PENDING_DELETES
        for real in pend:
            if real not in _PENDING_DELETES:
                _PENDING_DELETES.append(real)

        try:
            from guard import guard_gate as _gate

            for real in pend:
                _gate.intercept(real, f"影子里要删：{real}")
        except Exception:  # noqa: BLE001
            pass

    # ── 清影子 ──
    try:
        _sl.cleanup(work_dir)
    except Exception:  # noqa: BLE001
        pass

    if written or failed:
        try:
            msg = (
                f"\n[影子] 落地 {written} 个文件"
                + (f"，{failed} 个失败" if failed else "")
                + (f"；{len(pend)} 个删除等着你确认" if pend else "")
                + "\n"
            )
            if r is not None and getattr(r, "stdout", None) is not None:
                r.stdout = (r.stdout or b"") + msg.encode("utf-8")
        except Exception:  # noqa: BLE001
            pass

    return r


def run_command(command: str, cwd: str = "", timeout: int = 120) -> str:
    """跑一条命令。

    Windows 上走 cmd /c，别的系统走 sh -c。
    stdout 和 stderr 合在一起返回——模型需要看到错误信息才能自己纠。
    """
    work_dir = _resolve(cwd) if cwd else _WORKDIR
    if not os.path.isdir(work_dir):
        work_dir = _WORKDIR

    # 让命令里能直接用 $WORKDIR / %WORKDIR%
    env = dict(os.environ)
    env["WORKDIR"] = work_dir

    # ⚠️ 强制子进程用 UTF-8，不依赖系统代码页。
    #
    #    这一条是 CI 打出来的。Windows 的 GitHub runner 是英文环境，
    #    默认代码页 cp1252。cp1252 里根本没有中文字符，
    #    所以 `echo 中文测试` 出来是 `????`——**字节层面就已经丢了**，
    #    后面再怎么解码都救不回来。
    #
    #    在中文 Windows 上（代码页 936）能过，所以本机一直没暴露。
    #
    #    三个变量一起设：
    #        PYTHONIOENCODING          Python 子进程的 stdout/stderr
    #        PYTHONUTF8                Python 子进程的文件编码
    #        PYTHONLEGACYWINDOWSSTDIO  旧的 Windows stdio 行为，清掉
    #
    #    cmd.exe 本身不吃这些变量（它看代码页），
    #    那部分在 _build_args 里用 chcp 65001 处理。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONLEGACYWINDOWSSTDIO", None)

    # ⚠️ Windows 上走"写文件再执行"，不走命令行参数。
    #
    #    为什么：命令行参数在两处会丢中文——
    #
    #        1. 父进程用 CreateProcessW 传的是 Unicode，
    #           但 cmd.exe 拿到之后会按**当前代码页**转成 ANSI 再解析。
    #           英文 Windows 的代码页是 cp1252，没有中文字符 → 变成 ?
    #           而且这个转换发生在 chcp 生效之前——
    #           所以 "chcp 65001 & echo 中文" 也救不了。
    #
    #        2. 就算能救，CREATE_NO_WINDOW 起的新控制台默认代码页
    #           又是系统 ANSI 页，每开一次都要重新处理一遍。
    #
    #    改成：把整条命令写成 UTF-8 文件，再用
    #        cmd /c "chcp 65001 >nul & call 文件.bat"
    #    执行。这样命令行参数里只有 ASCII（文件名），
    #    中文全在文件里，代码页切换在读取文件之前就生效了。
    #
    #    这是 CI 打出来的：中文 Windows（936）上一直是绿的。
    # ── 两条路：有影子执行走影子，没有就走普通 ──
    #
    # ⚠️ 原来这里是：
    #        if sys.platform == "win32":   沙箱
    #        else:                         裸 subprocess.run
    #
    #    Linux 上一直是**裸跑**：没有影子层，也没有断网。
    #    现在 Linux 有 unshare + overlayfs 了，两边都走影子。
    _st = _sandbox_state()
    _use_box = bool(_st.get("ok"))

    if _use_box:
        try:
            r = _run_in_sandbox_if_possible(command, work_dir, env, timeout)
        except subprocess.TimeoutExpired:
            # 影子里跑超时了 —— 不要再跑一遍。
            # 重跑的话用户说 3 秒实际等 10 秒，而且命令可能又改一遍文件。
            return _timeout_msg(command, timeout)

        if r is None:
            # None 才是"影子执行不可用"。
            #
            # ⚠️ 这里原来是**无条件**退回普通执行 ——
            #    而普通执行是联网的（影子里断网）。
            #
            #    现在分两种：
            #        根本没装      → 允许（正常配置）
            #        装了但起不来  → 要么有联网授权，要么拒绝
            _can, _why = _net_fallback_allowed()
            if not _can:
                _sent, _msg = True, ""
                try:
                    from guard import netauth as _na

                    _sent, _msg = _na.send_code()
                except Exception as _e:  # noqa: BLE001
                    _sent, _msg = False, f"{type(_e).__name__}"

                return (
                    f"【需要联网授权】{_why}\n"
                    f"命令：{command[:200]}\n"
                    "\n"
                    "⚠️ 这条命令**没有执行**。影子执行起不来，"
                    "而普通执行是联网的 —— 不能默认放行。\n"
                    + (
                        "验证码已发到你的邮箱。回复它，"
                        "再选一个时长（30 分钟 / 2 小时 / 8 小时）。"
                        if _sent
                        else f"但验证码没发出去（{_msg}）。"
                             "去「设置 → 远程验证」把邮箱配好。"
                    )
                )
            try:
                print(f"[影子] 不可用，退回普通执行：{_why}", file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass

            # Linux 上退回普通执行，但沙箱准备过的影子层得清掉
            if (_st.get("kind") or "") == "linux":
                try:
                    from guard import sandbox_linux as _sl

                    _sl.cleanup(work_dir)
                except Exception:  # noqa: BLE001
                    pass

            r = _run_on_windows(command, work_dir, env, timeout)

        # ── Linux：影子层里的改动要在这里落地 ──
        #
        # ⚠️ Windows 那套在 _run_in_sandbox_if_possible 里
        #    已经调过 apply_after_run 了（回写 + 扣删除）。
        #    Linux 的语义不一样：改动在 upper 层，得这里回写。
        if (_st.get("kind") or "") == "linux":
            try:
                r = _linux_apply_changes(work_dir, r)
            except Exception as _e:  # noqa: BLE001
                try:
                    print(f"[影子] Linux 回写失败：{_e}", file=sys.stderr)
                except Exception:  # noqa: BLE001
                    pass
    else:
        # 没有影子执行：直接跑。
        #
        # ⚠️ Windows 上**必须**走 _run_on_windows，不能 _build_args + subprocess.run。
        #
        #    2026-11-03 CI 实测（windows-latest 两个版本都红）：
        #        echo 中文测试  →  输出 '??????'
        #
        #    原因：
        #        _build_args 返回的是 ["cmd", "/c", "echo 中文测试"]，
        #        中文在**命令行参数**里。
        #        cmd.exe 拿到命令行后先按当前代码页（runner 是 cp437/cp1252）
        #        把中文转成 ANSI —— 那一步中文就变成 ? 了，
        #        后面怎么解码都救不回来。
        #
        #    _run_on_windows 的做法不一样：
        #        命令写成 UTF-8 的 .bat 文件，
        #        命令行只传 ASCII（"chcp 65001 >nul & call 文件.bat"），
        #        中文全在文件里，chcp 在读取文件之前就生效了。
        #
        #    ⚠️ 这条路径以前走不到，因为 _sandbox_state() 总是 True
        #       （本机装了 Sandboxie 时）。这版加了安全模式判断 +
        #       available() 真探测之后，CI 上会经常落到这里 ——
        #       于是这个一直存在的编码 bug 暴露出来了。
        if sys.platform == "win32":
            r = _run_on_windows(command, work_dir, env, timeout)
        else:
            args = _build_args(command)
            try:
                r = subprocess.run(
                    args,
                    cwd=work_dir,
                    env=env,
                    capture_output=True,
                    timeout=_effective_timeout(timeout),
                    creationflags=_nowindow_flags(),
                )
            except subprocess.TimeoutExpired:
                return f"命令超时（{timeout} 秒）被中止：{command}"
            except OSError as e:
                return f"起不来：{e}"


    if r is None:
        return _timeout_msg(command, timeout)

    # ── 如果上面是"起不来"，_run_on_windows 会返回一个假的完成对象 ──
    if getattr(r, "_start_failed", False):
        return f"起不来：{getattr(r, '_err', '')}"

    out = _decode_output(r.stdout or b"")
    err = _decode_output(r.stderr or b"")
    text = out + (("\n[stderr]\n" + err) if err.strip() else "")
    text = text.strip() or "(没有输出)"

    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n…（输出太长，截断到 {MAX_OUTPUT_CHARS} 字符）"

    return f"[退出码 {r.returncode}]\n{text}"


# ----------------------------------------------------------------------
# 给模型看的工具说明
# ----------------------------------------------------------------------

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读一个文件的内容，带行号。改文件之前先读一下要改的地方。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "offset": {"type": "integer", "description": "从第几行开始读，默认 1"},
                    "limit": {"type": "integer", "description": "读多少行，默认全读"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "新建文件或整篇覆盖。只想改几行的话用 edit_file，别用这个。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string", "description": "完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "把文件里的一段原文换成新的。old 必须和原文完全一致"
                "（含缩进空格），建议先用 read_file 看清楚。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string", "description": "要被替换的原文"},
                    "new": {"type": "string", "description": "换成的内容"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换所有出现，默认只换第一处",
                    },
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "看一个目录里有什么文件。不确定路径时先用它探路。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "在目录里按内容搜索字符串，返回命中的文件和行号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "要搜的字符串"},
                    "path": {"type": "string", "description": "在哪个目录下搜，默认当前目录"},
                    "glob_filter": {"type": "string", "description": "只搜匹配的文件，比如 *.py"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "跑一条命令行命令，返回输出和退出码。"
                "适合跑测试、git、装依赖。别用它读写文件——那是别的工具的活。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "description": "在哪个目录下跑"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 120"},
                },
                "required": ["command"],
            },
        },
    },
]

_DISPATCH = {
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "list_dir": list_dir,
    "search_files": search_files,
    "run_command": run_command,
}


def execute(name: str, arguments: str) -> str:
    """执行一次工具调用。**这里是所有工具调用的单点收口。**

    模型的参数偶尔会出问题：JSON 不完整、字段名写错、类型不对。
    这里全部吞掉异常，把错误当成工具结果返回给它——
    模型看到"你参数写错了"会自己改，直接抛异常则整轮就断了。

    ⚠️ 动作守卫挂在这里，不是挂在 agent 循环里。

        红队测试指出的 F-02：

            以前 _check_action 只在 engine/agent.py 的工具循环里调。
            而 execute() 本身没有任何守卫。

            当时只有 agent 循环调 execute()，所以不是直接可利用。
            但这是纵深防御缺口——任何新调用方（桌面端、recover.py、
            未来的 HTTP API、测试脚本）都会完全绕过命令层拦截。

        实测验证：

            agent._check_action("run_command", '{"command":"python -c ..."}')
              → 拦住了

            tools.execute("run_command", 同一条)
              → 真执行了

        这跟项目自己的设计原则矛盾。task_perms.py 里写着：
            "检查点放在 tools.py 的 _resolve()——所有文件工具都走这条路，
              一个地方把关比每个工具各加一遍靠谱"

        路径层做到了单点收口，命令层没有。现在补上。

        为什么保留 agent 里那层：
            双层。execute() 这层保证"任何调用方都绕不过"；
            agent 那层负责给模型一段更友好的说明，并且能统计
            "这个任务里被拦了几次"。两层做的事不重复。
    """
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"没有这个工具：{name}"

    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as e:
        return (
            f"参数不是合法 JSON：{e}\n"
            f"收到的是：{arguments[:300]}\n"
            "注意 JSON 里字符串要用双引号，路径里的反斜杠要写成 \\\\"
        )

    if not isinstance(args, dict):
        return f"参数应该是个对象，收到的是 {type(args).__name__}"

    # 只保留函数认识的参数，多余的丢掉（模型有时会自作主张加字段）
    import inspect

    try:
        accepted = set(inspect.signature(fn).parameters)
        args = {k: v for k, v in args.items() if k in accepted}
    except (TypeError, ValueError):
        pass

    # ── 锁死检查：单点收口 ──
    #
    # ⚠️ 这一道必须放在最前面，比动作守卫还前。
    #
    #    规则（用户定的）：
    #        到 3 次 → 锁死 → 必须验证码
    #        **永远不可能超过 3**
    #
    #    原来只做到了"派不出去"，没做到"跑不了"：
    #        派活那道门查锁了（cmd_dispatch → gate）
    #        执行这道门（execute）没查
    #
    #    所以任何直接调 execute 的路径都能绕过锁：
    #        执行 → 触发拦截 → 计数 +1 → 涨到 6、9、12
    #
    #    这跟红队 F-02 是同一类错：
    #        当时说的是"守卫只挂在 agent 循环里，execute 是裸的"
    #        那次修了动作检查，漏了锁状态检查。
    #
    #    现在两道门都查，锁死才是真锁死。
    try:
        from guard import guard_gate as _gg

        _c = _gg.peek_count()
        if _c.get("tripped"):
            return (
                f"[锁死了] 任务端已经锁死（今天第 {_c.get('count')} 次被拦），"
                "任何工具都不会执行。\n"
                "要用的话先过验证码：码会发到配置的邮箱，"
                "拿到之后在桌面端「危险操作验证」里填进去。\n"
                "验证通过后计数清零，重新派活即可。"
            )
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        # 计数读不出来 —— 不能因此放行。
        # 这个判断跟 guard_gate.intercept 里"存储坏了按最严处理"一致。
        return (
            "[锁死了] 读不到拦截计数，按最严处理：工具不执行。\n"
            "去桌面端看一眼「危险操作验证」那栏。"
        )

    # ── 动作守卫：单点收口 ──
    #
    # 放在参数清洗之后、真正调用之前。
    # 这里用 late import 是为了避免循环依赖
    # （agent 会 import tools，tools 不该在顶层 import agent）。
    try:
        from engine import agent as _agent

        verdict = _agent._check_action(name, json.dumps(args))
        if verdict is not None:
            return verdict
    except ImportError:
        # agent 模块不可用（比如单独测 tools）——不因此放行。
        # 直接跑一个最小化的检查。
        try:
            from guard import action_guard as _ag

            if name == "run_command":
                cmd = str(args.get("command") or "")
                is_exec, label = _ag.uses_arbitrary_executor(cmd)
                if is_exec:
                    return _ag.explain_executor(label)
                hits = _ag.describe(cmd)
                if hits:
                    return _ag.explain_irreversible(hits[0])
        except ImportError:
            pass
    except Exception as e:  # noqa: BLE001
        # 守卫自己炸了 —— **不放行**。
        # 跟 _blocked() 里那个保守方向一致：守卫挂了不等于安全。
        return f"[拦下了] 动作守卫出错（{type(e).__name__}），这条不放行。"

    try:
        return str(fn(**args))
    except TypeError as e:
        return f"参数不对：{e}"
    except Exception as e:  # noqa: BLE001
        return f"执行出错：{type(e).__name__}: {e}"
