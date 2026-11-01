"""Sandboxie 影子执行：让"删"这个动作先在影子层发生。

为什么需要这一层
    守卫在这一层一直有个缺口：它能拦住命令行里"写着"的破坏，
    但拦不住"写个脚本再执行"——因为脚本内容是千变万化的，
    词表追不完，内容扫描也追不完（编码、拼接、动态生成）。

    而"删除"这个动作躲不掉：它一定落在文件系统上。
    所以换个位置看——不猜它要干什么，看它真干了什么。

    但没有影子层的话，"看它干了什么"这件事本身就已经伤到真文件了。
    沙箱把这个顺序倒过来：
        先让删发生在影子里（真文件不动）
        跑完看影子里记了哪些删除
        有删除 → 停下来要验证码
        没问题 → 才让真的发生

为什么用 Sandboxie 而不是 Docker/复制目录
    复制目录：一个 10G 的项目要复制一遍，多个任务同时跑就爆了；
              而且两个任务改同一个文件时，副本没法合并。
    沙箱：删除被记成一条"已删"标记，不移文件、不复制大文件。
          Windows 上这是唯一免驱动、免管理员账号的做法。
          Sandboxie 本身装一次要管理员，之后日常调用不需要。

⚠️ 这一层是可选的
    没装 Sandboxie 就退回原来的行为，不强制、不阻塞。
    但要在日志和界面上说清楚"当前没有影子保护"，
    不能让用户以为有。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time

# Sandboxie 的默认安装位置。用户装到别处也能找到——
# 下面 _find_install() 会去注册表问。
_DEFAULT_DIRS = (
    r"C:\Program Files\Sandboxie-Plus",
    r"C:\Program Files\Sandboxie",
)

# 我们用的沙箱名。起个能认出来的名字，别用 DefaultBox——
# 用户自己也可能用 DefaultBox 装别的程序，混在一起不好分。
BOX_NAME = "RemoteDispatch"

_install_cache: str | None = None
_install_checked = False


def _find_install() -> str:
    """找 Sandboxie 装在哪。找不到返回空串。

    先看注册表（用户可能装在别的盘），再看默认路径。
    """
    global _install_cache, _install_checked
    if _install_checked:
        return _install_cache or ""
    _install_checked = True

    # 注册表
    if sys.platform == "win32":
        try:
            import winreg

            for root in (
                winreg.HKEY_LOCAL_MACHINE,
                winreg.HKEY_CURRENT_USER,
            ):
                for sub in (
                    r"SOFTWARE\Sandboxie-Plus",
                    r"SOFTWARE\Sandboxie",
                ):
                    try:
                        with winreg.OpenKey(root, sub) as k:
                            d, _ = winreg.QueryValueEx(k, "InstallDir")
                            if d and os.path.exists(
                                os.path.join(d, "Start.exe")
                            ):
                                _install_cache = d
                                return d
                    except OSError:
                        continue
        except Exception:  # noqa: BLE001
            pass

    # 默认路径
    for d in _DEFAULT_DIRS:
        if os.path.exists(os.path.join(d, "Start.exe")):
            _install_cache = d
            return d

    _install_cache = ""
    return ""


def available() -> bool:
    """这一层能不能用。"""
    return sys.platform == "win32" and bool(_find_install())


def describe() -> str:
    """一句话说清当前状态。给界面和日志用。"""
    if sys.platform != "win32":
        return "影子执行：仅 Windows 支持"
    d = _find_install()
    if not d:
        return "影子执行：没装 Sandboxie，删除会直接生效"
    if not box_exists():
        return f"影子执行：Sandboxie 已装（{d}），沙箱还没建"
    return f"影子执行：已启用（沙箱 {BOX_NAME}）"


def _read_ini(path: str) -> str:
    """读 Sandboxie.ini。

    ⚠️ 这个文件是 **UTF-16** 的（Sandboxie 自己写的时候用 UTF-16）。
        按 UTF-8 读会得到一堆带 \\x00 的乱码，
        里面明明有 [RemoteDispatch] 也搜不到——
        实测踩过：沙箱建好了，box_exists() 却一直返回 False。
    """
    raw = open(path, "rb").read()
    # 有 BOM 或者大量 \x00 → 是 UTF-16
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    if raw.count(b"\x00") > len(raw) // 4:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def box_exists() -> bool:
    """我们的沙箱建了没。"""
    ini = _ini_path()
    if not ini or not os.path.exists(ini):
        return False
    try:
        return f"[{BOX_NAME}]" in _read_ini(ini)
    except OSError:
        return False


def _ini_path() -> str:
    """Sandboxie 的配置文件在哪。

    ⚠️ 两个位置：
        C:\\Windows\\Sandboxie.ini   系统级（管理员写的）
        用户目录下的         用户级
    """
    p = r"C:\Windows\Sandboxie.ini"
    if os.path.exists(p):
        return p
    return os.path.join(
        os.environ.get("APPDATA") or "", "Sandboxie", "Sandboxie.ini"
    )


def _detect_ini_encoding(path: str) -> str:
    """探一下 ini 是什么编码。

    Sandboxie.ini 在中文 Windows 上是 UTF-16 LE（带 BOM），
    但也见过 ANSI 的。写之前必须知道——
    往 UTF-16 的文件里追加 UTF-8 字节，写进去就是垃圾，
    Sandboxie 读不懂，box_exists() 恒 False，影子层静默不启用。
    """
    try:
        head = open(path, "rb").read(4)
    except OSError:
        return "utf-8"

    if head.startswith(b"\xff\xfe"):
        return "utf-16"
    if head.startswith(b"\xfe\xff"):
        return "utf-16"
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"

    # 没有 BOM：看有没有很多 \x00（UTF-16 里 ASCII 字符第二个字节是 0）
    try:
        raw = open(path, "rb").read(2048)
    except OSError:
        return "utf-8"
    if raw and raw.count(b"\x00") > len(raw) // 4:
        return "utf-16-le"
    return "utf-8"

def ensure_box() -> tuple[bool, str]:
    """确保沙箱存在。没有就建一个。

    返回 (成功, 说明)。

    ⚠️ 建沙箱要写系统级的 Sandboxie.ini，要管理员权限。
        第一次需要在界面上引导用户点一下提权。
        已经建过的话，这里是零开销。
    """
    if not available():
        return False, "没装 Sandboxie"
    if box_exists():
        return True, "沙箱已存在"

    ini = _ini_path()
    try:
        if not os.path.exists(ini):
            return False, f"找不到配置文件：{ini}"

        # ⚠️ 必须按文件**原来的编码**读写。
        #
        #    这个文件在这个模块里已经被坑过一次：
        #    _read_ini 的注释写着"Sandboxie.ini 是 UTF-16，
        #    按 UTF-8 读搜不到 [RemoteDispatch]，
        #    实测沙箱建好却显示没建"。
        #
        #    而这里写的时候用的还是 encoding="utf-8" ——
        #    往一个 UTF-16 的文件里追加 UTF-8 字节，
        #    写进去的就成了垃圾：Sandboxie 读不懂，
        #    box_exists() 恒 False，影子层静默不启用。
        #
        #    现在：先探编码，按同一个编码读全文、改、写回。
        enc = _detect_ini_encoding(ini)
        with open(ini, "r", encoding=enc, errors="replace") as f:
            body = f.read()

        if f"[{BOX_NAME}]" not in body:
            body = body.rstrip() + (
                f"\n[{BOX_NAME}]\n"
                "Enabled=y\n"
                "AutoDelete=n\n"
                "ConfigLevel=9\n"
            )
            with open(ini, "w", encoding=enc) as f:
                f.write(body)
        return True, f"沙箱已建（按 {enc} 写）"
    except PermissionError:
        return False, "没有管理员权限，建不了沙箱"
    except OSError as e:
        return False, f"建沙箱失败：{e}"


def _start_exe() -> str:
    d = _find_install()
    return os.path.join(d, "Start.exe") if d else ""


# ══════════════════════════════════════════════════════
# 跑
# ══════════════════════════════════════════════════════


def _shadow_of(real_path: str) -> str:
    r"""真机路径 → 沙箱里的对应路径。

    Sandboxie 的用户级沙箱把家目录镜像到
        C:\Sandbox\<用户名>\<沙箱名>\user\current\
    并把 "C:\Users\<用户名>\" 这一段映射成 "user\current\"。

    不在家目录下的路径（比如 D:\）在沙箱里是
        C:\Sandbox\<用户名>\<沙箱名>\drive\D\...
    """
    root = _box_root()
    home = os.path.expanduser("~")
    rp = os.path.abspath(real_path)

    if rp.lower().startswith(home.lower() + os.sep):
        rel = rp[len(home) + 1:]
        return os.path.join(root, "user", "current", rel)

    # 别的盘
    drive, rest = os.path.splitdrive(rp)
    if drive:
        letter = drive.rstrip(":").replace("\\", "\\")
        return os.path.join(root, "drive", letter, rest.lstrip("\\"))

    return ""


def run_in_box(
    command: str,
    cwd: str,
    env: dict,
    timeout: int,
    capture: bool = True,
) -> subprocess.CompletedProcess | None:
    """在沙箱里跑一条命令。

    command 一条完整命令（交给 cmd /c）
    cwd     工作目录
    env     环境变量
    timeout 超时秒数

    返回 CompletedProcess；沙箱不可用时返回 None，
    调用方据此退回普通执行。

    ⚠️ 输出怎么拿（这一段踩了三次，都记下来）：
        Start.exe 是 GUI 程序，它的 stdout 永远是空的。
        所以把命令的输出重定向到**一个文件**，
        然后按沙箱路径映射去读那个文件。

        实测确认：沙箱里写的文件，真机上没有，
                  但 C:\\Sandbox\\... 下面那份读得到。
    """
    start = _start_exe()
    if not start:
        return None

    out_real = ""
    cmd_body = command

    if capture:
        # 输出文件的真机路径（沙箱里会镜像到这个位置）
        tmp_dir = os.environ.get("TEMP") or os.path.expanduser("~")
        stamp = f"{os.getpid()}_{int(time.time() * 1000)}"
        out_real = os.path.join(tmp_dir, f"dsh_box_{stamp}.out")

        # ⚠️ 中文会乱码，必须切代码页。
        #
        #    实测：echo 中文 在沙箱里跑出来是 ????
        #    因为沙箱新建的 cmd 用的是系统 ANSI 代码页（中文 Windows 是 936，
        #    但我们读文件用 UTF-8，两边对不上）。
        #
        #    解决：命令前先 chcp 65001，让 cmd 输出 UTF-8。
        #    这跟 engine/tools.py 里 _run_on_windows 的做法一致。
        #
        # ⚠️ 多行输出丢失，也不能直接 `cmd && cmd`。
        #    实测：'echo A && echo B && exit /b 3' 输出是空的。
        #
        #    解决：把整条命令写成 .bat 文件，再 call 它。
        #
        # ⚠️ .bat 必须写进**沙箱目录**，不是真机目录。
        #    实测：写到真机路径，沙箱里 call 它 → 退出码 0x40010004
        #          （调试器终止），什么都不执行。
        #          因为沙箱里根本没有那个文件——它只在真机上。
        #
        #    所以：先算沙箱路径，把 .bat 直接写在那里，再 call。
        bat_shadow = ""
        try:
            bat_shadow = _shadow_of(
                os.path.join(tmp_dir, f"dsh_box_{stamp}.bat")
            )
            os.makedirs(os.path.dirname(bat_shadow), exist_ok=True)
            # ⚠️ 编码踩了这一串坑，结论是：**用 ANSI（GBK）写**。
            #
            #    三次实测：
            #      1. 纯 UTF-8 无 BOM + 中文
            #         → cmd 按 936 读，中文字节错乱，卡住后超时
            #      2. UTF-8 带 BOM
            #         → cmd 把 BOM 当成命令的一部分
            #           输出变成 "\ufeff@echo off"，命令没执行
            #      3. ANSI(GBK)
            #         → 中文正常，跟 cmd 的默认代码页一致
            #
            #    所以在中文 Windows 上用 GBK 写；
            #    写不了（字符在 GBK 里没有）就退回 UTF-8。
            body = "@echo off\r\n" + command + "\r\n"
            enc = "utf-8"
            try:
                body.encode("gbk")
                enc = "gbk"
            except UnicodeEncodeError:
                pass
            with open(bat_shadow, "w", encoding=enc, newline="\r\n") as f:
                f.write(body)
            use_chcp = enc == "utf-8"
        except OSError:
            bat_shadow = ""
            use_chcp = False

        esc_out = out_real.replace('"', '""')
        if bat_shadow:
            esc_bat = bat_shadow.replace('"', '""')
            pre = "chcp 65001 >nul && " if use_chcp else ""
            cmd_body = f'{pre}call "{esc_bat}" > "{esc_out}" 2>&1'
        else:
            esc_cmd = command.replace('"', '""')
            cmd_body = f'{esc_cmd} > "{esc_out}" 2>&1'

    # ⚠️ 必须传一整条命令行字符串 + shell=True。
    #
    #    给参数列表的话，Start.exe 自己再拼一遍，引号层级错乱，
    #    结果是一条语法错误但静默失败的命令（返回 0，什么都没做）。
    # 三个参数都是官方文档里的，各有各的用处：
    #
    #   /box:X         指定沙箱
    #   /hide_window   不显示沙箱程序的窗口
    #                  ——不加这个，每跑一条命令弹一个黑框。
    #                    实测：跑一遍回归弹了 347 次。
    #   /wait          等沙箱里的程序真正结束才返回
    #                  ——不加这个，Start.exe 先返回，
    #                    里面的进程还占着工作目录和临时文件。
    esc = cmd_body.replace('"', '""')
    cmd_line = (
        f'"{start}" /box:{BOX_NAME} /hide_window /wait '
        f'cmd.exe /c "{esc}"'
    )

    try:
        p = subprocess.run(
            cmd_line,
            shell=True,
            cwd=cwd,
            env=env,
            timeout=timeout,
            capture_output=False,
        )
    except subprocess.TimeoutExpired:
        raise
    except (OSError, subprocess.SubprocessError):
        return None

    # ⚠️ Start.exe 返回得比里面的 cmd.exe 早。
    #
    #    实测：命令跑完了、输出也拿到了，但沙箱里的进程还占着
    #    工作目录几秒——调用的地方想删那个临时目录时，
    #    报 WinError 32「另一个程序正在使用此文件」。
    #
    #    等待策略：
    #        先短等 0.4 秒（大多数命令够）
    #        如果这次传了 cwd，再确认那个目录能不能写
    #        ——写不进去说明还有进程占着，继续等
    #
    #    上限 5 秒。超过就不再等：宁可让调用方看到一次清理失败，
    #    也不能把每条命令都拖成慢动作。
    time.sleep(0.4)
    if cwd and os.path.isdir(cwd):
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                probe = os.path.join(cwd, ".dsh_busy_probe")
                with open(probe, "wb"):
                    pass
                os.remove(probe)
                break
            except OSError:
                time.sleep(0.2)

    if capture and out_real:
        # 按沙箱映射读输出文件。
        #
        # ⚠️ 编码不是 UTF-8，是 **GBK**。
        #
        #    实测：沙箱里 `echo 中文 > file` 写出来的字节是
        #          d6 d0 ce c4——那是 GBK 的"中文"，不是 UTF-8。
        #    按 UTF-8 读会失败，读出来是空字符串。
        #    这就是"中文命令在沙箱里没输出来"的真正原因。
        #
        #    顺序：先试 UTF-8（无歧义时它对），失败退回 GBK。
        #    不能用 errors="replace" 硬读——那样会得到一串問號，
        #    看着像输出坏了一半。
        shadow = _shadow_of(out_real)
        text = ""

        # ⚠️ 重试逻辑要"等到有内容"，不能"读一次空就算完"。
        #
        #    踩过：文件出现了但还没写完，读到 0 字节就直接退出循环，
        #    于是输出是空的。表现是时好时坏（取决于命令跑多快）。
        #
        #    现在：最多等 3 秒，每次 0.12 秒，读到非空才算成功。
        deadline = time.time() + 3.0
        while time.time() < deadline:
            got_raw = b""
            for cand in (shadow, out_real):
                if not cand or not os.path.exists(cand):
                    continue
                try:
                    got_raw = open(cand, "rb").read()
                except OSError:
                    continue
                if got_raw:
                    break
            if got_raw:
                for enc in ("utf-8", "gbk", "utf-16"):
                    try:
                        text = got_raw.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                if text:
                    break
            time.sleep(0.12)

        # subprocess.CompletedProcess 的 stdout 属性可以直接赋。
        # （capture_output=False 时它是 None，直接换掉就行。）
        p.stdout = text
        p.stderr = ""

        # 清掉自己造的输出文件（两边都试）
        for cand in (shadow, out_real):
            try:
                if cand and os.path.exists(cand):
                    os.remove(cand)
            except OSError:
                pass

        # .bat 也要清。它写在沙箱里，真机路径上没有。
        try:
            if bat_shadow and os.path.exists(bat_shadow):
                os.remove(bat_shadow)
        except OSError:
            pass

    return p

# ══════════════════════════════════════════════════════
# 影子账本：它想删哪些东西
# ══════════════════════════════════════════════════════


def _box_root() -> str:
    """沙箱的物理目录。

    每个沙箱落在 Sandbox 根目录下「用户名/沙箱名」的位置。
    用户级沙箱在 user 下面的 current 里镜像了他的目录结构。
    """
    user = os.environ.get("USERNAME") or ""
    return os.path.join(r"C:\Sandbox", user, BOX_NAME)


def list_deferred_deletes() -> list[dict]:
    """影子层里"它要删、但还没真删"的东西。

    怎么认出来的：
        Sandboxie 的延迟删除不是把文件挪走，
        而是在沙箱目录里给那个路径建一个**同名但内容为空/占位**的项，
        同时真文件不动。

    这里的做法更稳一点：直接比对"沙箱目录里有哪些路径"和
    "真机上对应的路径还在不在"。对得上就说明是"被影子删了"。

    ⚠️ 这是个近似判断，不是内核级精确。
        但用它来判断"这次跑有没有发生删除"足够了——
        因为我们只需要知道"有没有可能需要用户确认"，
        不需要精确到字节。
    """
    root = _box_root()
    if not os.path.isdir(root):
        return []

    out: list[dict] = []

    # ⚠️ 要扫**所有**盘，不能只扫 C。
    #
    #    原来写死了两个 base：user/current 和 drive/C。
    #    于是 D 盘、E 盘上的删除根本进不了这个列表——
    #    那只"新眼睛"恰好在最常见的场景（项目在 D 盘）上失明。
    #
    #    实测：workspaces 里一个 D:\\Project 的活，删了东西，
    #    这里出来是空列表。
    #
    #    现在的做法：把 drive 下所有子目录都收进来。
    bases = [os.path.join(root, "user", "current")]
    drive_root = os.path.join(root, "drive")
    if os.path.isdir(drive_root):
        try:
            for d in sorted(os.listdir(drive_root)):
                p = os.path.join(drive_root, d)
                if os.path.isdir(p):
                    bases.append(p)
        except OSError:
            pass

    for base in bases:
        if not os.path.isdir(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                shadow = os.path.join(dirpath, fn)
                # 还原成真机路径
                rel = os.path.relpath(shadow, base)
                real = _real_path_of(rel, base)
                if not real:
                    continue
                out.append(
                    {
                        "shadow": shadow,
                        "real": real,
                        "exists_on_disk": os.path.exists(real),
                    }
                )
    return out


def _real_path_of(rel: str, base: str) -> str:
    """把沙箱里的相对路径换算成真机路径。

    ⚠️ 原来只认 drive 下的 c 盘，别的盘一律返回空串。

        后果很隐蔽：D 盘、E 盘上的删除检不出来——
        那只"新眼睛"恰好在最常见的场景（项目在 D 盘）上失明。
        而且 D 盘上的写入也留在沙箱里回不到真机，
        任务报成功、用户看不到文件。

        实测：_real_path_of('Project\\a.docx', '...\\drive\\D') → ''

        README 和提交示例里用的就是 D:\\Project，
        所以这条几乎必然被撞上。

    现在：从 base 里把盘符抠出来，通用处理。
    """
    low = base.replace("/", "\\").lower()

    if "\\user\\current" in low:
        home = os.path.expanduser("~")
        return os.path.join(home, rel)

    # 通用：drive\<字母> → <字母>:\
    m = re.search(r"\\drive\\([a-z])", low)
    if m:
        letter = m.group(1).upper()
        return os.path.join(letter + ":\\", rel)

    return ""


def pending_deletes(baseline: set[str] | None = None) -> list[str]:
    """这次跑里，被影子层挡下的删除。

    ⚠️ 判据演进过一次，记一下为什么。

        原来是"沙箱里有 + 真机上没有"。
        漏了一种情况：删掉之后又建一个同名的——
        真机上又有了，判据不成立，删除被漏掉。

        现在改成"沙箱里出现过的路径都算被动过"。
        因为沙箱是影子层：一个路径出现在里面，
        只可能是沙箱里的程序试图动那个位置。

    baseline 是"跑之前沙箱里已有的路径集合"。
        传进来的话，只返回这次新增的。
        不传就返回全部——第一次用的时候就是这样。

    区分删和写：
        沙箱里那份如果**是空的**，而真机上那份**还在且非空**，
        说明它想清空/删除那个文件。
        这个判断是近似的，但足够用来决定"要不要问用户"。
    """
    items = list_deferred_deletes()
    seen: set[str] = set()
    out: list[str] = []

    for it in items:
        real = it["real"]
        if not real or real in seen:
            continue
        seen.add(real)

        if baseline is not None:
            shadow = it["shadow"]
            if shadow in baseline:
                continue  # 上一轮就在，不是这次的

        # 沙箱里有这个路径 → 这次动过它
        #
        # 再分两种情况：
        #   真机上那份还在   → 可能是"删了又建"或"写了个空的"
        #   真机上那份没了   → 明确是删除（真文件还在，只是被影子挡了）
        out.append(real)

    return sorted(out)


def snapshot() -> set[str]:
    """记下现在沙箱里有哪些路径。跑之前调一次，跑完对比。

    为什么需要基线：
        沙箱不是每轮清空的（AutoDelete=n）。
        上一轮留下的痕迹会混进这一轮的判断里。
        记个基线，只看新增的。
    """
    return {it["shadow"] for it in list_deferred_deletes()}


def clear_box() -> bool:
    """清空沙箱内容（丢弃这次跑的痕迹）。"""
    root = _box_root()
    if not os.path.isdir(root):
        return True
    # 安全检查：只删 C:\Sandbox 下面的
    if not root.lower().startswith("c:\\sandbox\\"):
        return False
    try:
        shutil.rmtree(root, ignore_errors=True)
        return True
    except OSError:
        return False


# ══════════════════════════════════════════════════════
# 回写：写入/修改落回真机，删除扣住
# ══════════════════════════════════════════════════════


def classify_changes(baseline: set[str] | None = None) -> dict:
    """把影子层里这次新增的改动分三类。

    返回：
        {
            "deletes":  [真机路径...]   要扣住的
            "writes":   [(影子路径, 真机路径)...]   新写入的
            "modifies": [(影子路径, 真机路径)...]   改过的
        }

    baseline 传了的话，只算这次新增的（上一轮的不算）。
    """
    out = {"deletes": [], "writes": [], "modifies": []}

    for it in list_deferred_deletes():
        shadow = it["shadow"]
        real = it["real"]
        if not real:
            continue
        if baseline is not None and shadow in baseline:
            continue

        try:
            ssize = os.path.getsize(shadow)
        except OSError:
            continue

        real_exists = os.path.exists(real)

        if not real_exists:
            # 真机上没有 → 新写入
            out["writes"].append((shadow, real))
            continue

        try:
            rsize = os.path.getsize(real)
        except OSError:
            rsize = -1

        if ssize == 0 and rsize > 0:
            # 真机有内容、影子里是空的 → 删除
            out["deletes"].append(real)
        elif ssize == 0 and rsize == 0:
            # 两边都是空的 → 分不清，当成删除（宁可多问一次）
            out["deletes"].append(real)
        else:
            # 影子里有内容 → 修改（内容一样也算改过，回写是幂等的）
            out["modifies"].append((shadow, real))

    return out


def recover_writes(changes: dict) -> dict:
    """把写入和修改落回真机。删除不动。

    ⚠️ 回写前要过权限检查。

        这里原来是无条件 copy2：影子里有写，就复制到真机。
        于是在"工作区"权限下，脚本往 %USERPROFILE%/Desktop 写的东西
        会被自动落地 —— 文件工具走不过去的边界，脚本写过去就过了。

        这跟 task_perms 那道边界是矛盾的：
            文件工具 → _resolve → is_allowed → 拒
            脚本写   → 影子 → recover_writes → 直接落地

        现在的规则：
            能落地  工作区里的、或者当前权限档允许的位置
            不落地  权限外的东西——写进去了也留在沙箱里，
                    并且明确告诉调用方"这几条因为权限没落地"。

        为什么不是"拦住不执行"：
            沙箱里执行的时候不知道会遇到什么，
            只能事后按结果分类。所以这里做的是"事后过滤"。

    返回 {"ok": [...], "fail": [...], "denied": [(路径, 原因)...]}。
    """
    ok: list[str] = []
    fail: list[tuple[str, str]] = []
    denied: list[tuple[str, str]] = []

    # 拿当前任务的工作区和权限档
    workdir = ""
    level = "workspace"
    try:
        from engine import tools as _tools
        from guard import task_perms as _tp

        workdir = _tools.workdir() or ""
        level = getattr(_tools, "_LEVEL", "workspace")
        _ = _tp  # 只是确认能导入
    except Exception:  # noqa: BLE001
        pass

    def _allowed(real_path: str) -> tuple[bool, str]:
        # 这条路径准不准落回真机
        if not workdir:
            # 没有工作区信息 —— 不能瞎放
            return False, "不知道任务的工作区，不敢落地"
        try:
            from guard import task_perms as _tp

            if _tp.is_allowed(real_path, level, workdir):
                return True, ""
            return False, f"在「{level}」权限范围外"
        except Exception as e:  # noqa: BLE001
            # 判不出来就按最坏情况：不落地
            return False, f"权限判断出错（{type(e).__name__}），按最坏处理"

    for pair in changes.get("writes", []) + changes.get("modifies", []):
        shadow, real = pair
        okk, why = _allowed(real)
        if not okk:
            denied.append((real, why))
            continue
        try:
            parent = os.path.dirname(real)
            if parent:
                os.makedirs(parent, exist_ok=True)
            # 用 copy2 保留时间戳——用户在资源管理器里看到的
            # 修改时间不会因为我们绕了一圈而变
            shutil.copy2(shadow, real)
            ok.append(real)
        except OSError as e:
            fail.append((real, str(e)))

    return {"ok": ok, "fail": fail, "denied": denied}


def apply_after_run(baseline: set[str] | None = None) -> dict:
    """跑完命令后的统一处理。

    1. 分类
    2. 写入/修改 → 回写（用户无感）
    3. 删除 → 留在返回的列表里，等验证码

    返回：
        {
            "recovered": [回写成功的真机路径...],
            "deletes":   [需要用户确认的删除...],
            "failed":    [(路径, 原因)...],
        }
    """
    changes = classify_changes(baseline)
    rec = recover_writes(changes)
    return {
        "recovered": rec["ok"],
        "deletes": changes["deletes"],
        "failed": rec["fail"],
        # 权限外、没落地的那几条——要让调用方知道，
        # 不然用户以为写进去了，其实是空的
        "denied": rec.get("denied") or [],
    }


def clean_written_shadows(baseline: set[str] | None = None) -> int:
    """清掉已经回写过的影子文件。

    不清的话，下一轮会再把它们当成"改动"处理一遍。
    （回写是幂等的，重复几次不会错，但会越来越慢。）
    """
    n = 0
    for it in list_deferred_deletes():
        shadow = it["shadow"]
        if baseline is not None and shadow in baseline:
            continue
        real = it["real"]
        if not real:
            continue
        # 只有"真机和影子都有内容"的才清——那是已经回写过的
        try:
            if not os.path.exists(real):
                continue
            if os.path.getsize(shadow) == 0:
                continue
            os.remove(shadow)
            n += 1
        except OSError:
            continue
    return n

def commit_deferred(paths: list[str]) -> list[tuple[str, bool]]:
    r"""把"要删的"真的删掉。

    这是验证码通过之后才做的事。
    返回 [(路径, 成功)]。

    ⚠️ 两件事必须一起做，否则会死循环：

        1. 在**沙箱外**真删（os.remove / rmtree）——
           在沙箱里删的话，又被影子层接住，等于没删

        2. 把沙箱里对应的影子记录**清掉**——
           不清的话，下次检测又会看到它，
           又弹一次验证码，用户确认了又删不掉，无限循环

    （这是用户实测出来的：确认之后还是删不掉。根因就是第 1 步
      和沙箱混在一起了，加上没清影子。）
    """
    out: list[tuple[str, bool]] = []
    root = _box_root()

    for p in paths:
        if not p:
            continue
        # 安全底线：不许删盘符根、不许删太浅的路径
        norm = os.path.abspath(p)
        if len(norm) <= 4 or re.match(r"^[A-Za-z]:\\?$", norm):
            out.append((p, False))
            continue

        try:
            if os.path.isdir(norm):
                shutil.rmtree(norm, ignore_errors=True)
            elif os.path.exists(norm):
                os.remove(norm)
            ok = not os.path.exists(norm)
        except OSError:
            ok = False

        # 清影子：真机路径 → 沙箱路径，那边那条记录也删掉。
        # 不管真删成没成，影子都清——否则会一直重复检测到。
        try:
            shadow = _shadow_of(norm)
            if shadow and shadow.lower().startswith(root.lower()):
                if os.path.isdir(shadow):
                    shutil.rmtree(shadow, ignore_errors=True)
                elif os.path.exists(shadow):
                    os.remove(shadow)
        except OSError:
            pass

        out.append((p, ok))

    return out
