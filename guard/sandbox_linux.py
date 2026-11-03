"""Linux 影子执行：靠 unshare + overlayfs，不需要装任何东西。

## 跟 Windows 版（sandbox.py）的关系

    同一套语义，两种实现：

        Windows    Sandboxie 的延迟删除 + 影子文件
        Linux      内核的 namespace + overlayfs

    对外接口一样：
        available()          能不能用
        run_in_box()         在影子里跑一条命令
        classify_changes()   这次改了 / 删了 / 新建了什么
        apply_after_run()    按审核结果决定落不落地
        commit_deferred()    用户确认后，把删除真的做掉

## 原理

    unshare --user --map-root-user --mount --net
        --user   普通用户也能建命名空间，不用 sudo
        --mount  挂载隔离（overlay 只在这个命名空间里可见）
        --net    顺便断网

    然后在这个命名空间里挂一层 overlayfs：

        lowerdir   真机目录的**拷贝**（只读）
        upperdir   改动层（可写）
        workdir    overlay 的工作目录
        merged     程序看到的那个目录

    程序在 merged 里改东西 → 落在 upperdir。
    退出命名空间，挂载消失，真机**一个字节都没动**。

## ⚠️ 为什么用"拷贝"当 lowerdir，不用真机目录

    理论上 lowerdir 可以直接指真机目录（只读）。
    但那样有个问题：程序读到的永远是**真机当前的样子**，
    如果程序在影子里改完又读，读到的是 overlay 合并后的视图 ——
    这没问题。

    真正的问题是 **delete**：
    在 overlay 上删一个 lower 里的文件，会生成 whiteout。
    如果 lower 是真机目录，whiteout 只在上层，安全。

    但为了两个平台语义**完全对齐**（Windows 版是拷贝），
    这里也用拷贝。代价是任务目录要复制一份。

    对任务目录（通常几 MB 到几十 MB）可以接受。
    真要优化，以后可以改成 lowerdir 直指真机。

## ⚠️ 能拦什么、不能拦什么

    拦住
        · 改文件    真机不动
        · 删文件    真机不动
        · 建文件    真机不动
        · 联网      --net 断掉

    拦不住
        · 读到敏感文件（读是允许的，这是设计）
        · 用绝对路径写别处（会被 overlay 拦住，但也只是"拦住"）
        · 内核漏洞

## 依赖

    unshare    util-linux 自带（几乎所有发行版都有）
    overlayfs  内核自带（Linux 3.18+）

    实测环境（2026-11-03）：
        WSL2 Ubuntu 24.04 / 内核 6.18.33
        user namespace      OK
        mount namespace     OK
        net namespace       OK
        overlayfs           内核支持
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

# 影子根目录。每个任务一个子目录。
SHADOW_ROOT_NAME = ".dsh_shadow"

# overlay 里的 whiteout 文件特征
# Linux 用字符设备 0:0 表示"这个文件被删了"。
WHITEOUT_DEV = (0, 0)


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def available() -> bool:
    """这套能不能用。

    ⚠️ 逐个检查，而不是"猜"。
        少一样就明确说少哪样，比返回一个 False 有用得多。
    """
    if not _is_linux():
        return False

    if not shutil.which("unshare"):
        return False

    # overlayfs 支不支持
    try:
        with open("/proc/filesystems", encoding="utf-8") as f:
            if "overlay" not in f.read():
                return False
    except OSError:
        return False

    return True


def describe() -> str:
    """一句话说清楚这套的状态。给界面用。"""
    if not _is_linux():
        return "不是 Linux，用不了这套"
    if not shutil.which("unshare"):
        return "缺 unshare（装 util-linux）"
    try:
        with open("/proc/filesystems", encoding="utf-8") as f:
            if "overlay" not in f.read():
                return "内核不支持 overlayfs"
    except OSError:
        return "读不到 /proc/filesystems"
    return "可用（unshare + overlayfs，内核级）"


# ══════════════════════════════════════════════════════
# 影子目录
# ══════════════════════════════════════════════════════

def _shadow_dir(workdir: str) -> str:
    """这个任务用哪个影子目录。"""
    base = os.path.join(os.path.dirname(os.path.abspath(workdir)), SHADOW_ROOT_NAME)
    name = os.path.basename(os.path.abspath(workdir))
    return os.path.join(base, name)


def _layers(workdir: str) -> dict:
    """影子目录里各部分的路径。

    ⚠️ current 是"这次改动的落点"（可写）。
        它**不能**同时出现在 lowerdir 里 ——
        overlay 不允许一个目录既当 lower 又当 upper，
        否则报 "Too many levels of symbolic links"。

        实测（2026-11-03）踩过：lowerdir=v1:base，upperdir=v1。
    """
    root = _shadow_dir(workdir)
    return {
        "root": root,
        "base": os.path.join(root, "base"),
        "current": os.path.join(root, "current"),
        "work": os.path.join(root, "work"),
        "merged": os.path.join(root, "merged"),
    }

def _versions(workdir: str) -> list[str]:
    """所有版本目录（v1, v2, ...），按编号从旧到新。"""
    root = _shadow_dir(workdir)
    if not os.path.isdir(root):
        return []

    out = []
    for name in os.listdir(root):
        m = re.match(r"^v(\d+)$", name)
        if m:
            out.append((int(m.group(1)), os.path.join(root, name)))
    out.sort()
    return [p for _n, p in out]


def _next_version_dir(workdir: str) -> str:
    """下一个版本目录（不创建）。"""
    root = _shadow_dir(workdir)
    vs = _versions(workdir)
    n = len(vs) + 1
    return os.path.join(root, f"v{n}")


def prepare(workdir: str) -> tuple[bool, str]:
    """准备影子层。

    每次跑之前：
        一、把上一次的 current **冻结**成 v(N+1)（如果它非空）
        二、清空 current（下次改动的落点）
        三、base 只在第一次建

    ⚠️ current 不能出现在 lowerdir 里 —— 见 _layers 的说明。
    """
    if not available():
        return False, describe()

    if not os.path.isdir(workdir):
        return False, f"工作目录不存在：{workdir}"

    L = _layers(workdir)

    try:
        os.makedirs(L["root"], exist_ok=True)
        os.makedirs(L["work"], exist_ok=True)
        os.makedirs(L["merged"], exist_ok=True)

        # base 只在第一次建。之后永远沿用 —— 它就是"最初的样子"。
        if not os.path.isdir(L["base"]):
            os.makedirs(L["base"], exist_ok=True)
            for name in os.listdir(workdir):
                src = os.path.join(workdir, name)
                dst = os.path.join(L["base"], name)
                try:
                    if os.path.isdir(src) and not os.path.islink(src):
                        shutil.copytree(src, dst, symlinks=True)
                    else:
                        shutil.copy2(src, dst, follow_symlinks=False)
                except (OSError, shutil.Error):
                    continue

        # 上一轮的 current：非空就冻结成新版本
        if os.path.isdir(L["current"]) and os.listdir(L["current"]):
            _freeze_current(workdir)

        # 清空（或新建）current
        shutil.rmtree(L["current"], ignore_errors=True)
        os.makedirs(L["current"], exist_ok=True)

        # 清掉上一轮的 merged / work（overlay 要求这俩是空的）
        shutil.rmtree(L["merged"], ignore_errors=True)
        shutil.rmtree(L["work"], ignore_errors=True)
        os.makedirs(L["merged"], exist_ok=True)
        os.makedirs(L["work"], exist_ok=True)

        n = len(_versions(workdir)) + 1
        return True, f"影子层准备好了（当前是第 {n} 轮）"
    except OSError as e:
        return False, f"准备影子层失败：{e}"


def _freeze_current(workdir: str) -> str:
    """把 current 冻结成一个版本目录。返回那个目录。"""
    L = _layers(workdir)
    n = len(_versions(workdir)) + 1
    dst = os.path.join(L["root"], f"v{n}")
    shutil.copytree(L["current"], dst, symlinks=True)
    return dst
def cleanup(workdir: str, keep_history: bool = True) -> None:
    """收拾影子层。

    ⚠️ 默认**保留历史**。

        以前是整个删 —— 那历史就没了。
        现在的"清理"只做两件事：
            一、清掉 merged / work（那两个是运行时用的，留着没用）
            二、保留 base 和所有 vN

    keep_history=False 才会整个删。
    """
    L = _layers(workdir)

    shutil.rmtree(L["merged"], ignore_errors=True)
    shutil.rmtree(L["work"], ignore_errors=True)
    os.makedirs(L["merged"], exist_ok=True)
    os.makedirs(L["work"], exist_ok=True)

    if not keep_history:
        shutil.rmtree(L["root"], ignore_errors=True)


# ══════════════════════════════════════════════════════
# 跑命令
# ══════════════════════════════════════════════════════

def build_script(workdir: str, command: str) -> str:
    """拼出要交给 bash 的脚本。

    ⚠️ 结构：

        lowerdir = vN:v(N-1):...:v1:base     （全是只读的历史层）
        upperdir = current                    （这次改动落点）

        current **绝对不能**出现在 lowerdir 里 ——
        那样 overlay 会报 "Too many levels of symbolic links"。
    """
    L = _layers(workdir)

    versions = _versions(workdir)
    layers = list(reversed(versions)) + [L["base"]]
    lowerdir = ":".join(layers)

    return f"""set -e
mount -t overlay overlay \\
    -o lowerdir={lowerdir},upperdir={L["current"]},workdir={L["work"]} \\
    {L["merged"]}
cd {L["merged"]}
{command}
"""


def _ls_in_overlay(lowerdir: str, upper: str, work: str, merged: str,
                   tmp_root: str) -> list[str]:
    """在临时 overlay 里列出所有文件。"""
    script = (
        f"mount -t overlay overlay -o "
        f"lowerdir={lowerdir},upperdir={upper},workdir={work} {merged}\n"
        f"cd {merged}\n"
        f"find . -type f -printf '%P\\n'\n"
    )
    sp = os.path.join(tmp_root, "ls.sh")
    with open(sp, "w", encoding="utf-8", newline="\n") as f:
        f.write(script)

    r = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--mount", "--", "bash", sp],
        capture_output=True, timeout=120,
    )
    out = (r.stdout or b"").decode("utf-8", "replace")
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _cp_in_overlay(lowerdir: str, upper: str, work: str, merged: str,
                   tmp_root: str, rel: str, workdir: str) -> bool:
    """在临时 overlay 里把一个文件拷回真机。"""
    real = os.path.join(workdir, rel)
    shadow = os.path.join(merged, rel)

    try:
        os.makedirs(os.path.dirname(real), exist_ok=True)
    except OSError:
        pass

    script = (
        f"mount -t overlay overlay -o "
        f"lowerdir={lowerdir},upperdir={upper},workdir={work} {merged}\n"
        f'cp -p "{shadow}" "{real}" 2>/dev/null || cp "{shadow}" "{real}"\n'
    )
    sp = os.path.join(tmp_root, "cp.sh")
    with open(sp, "w", encoding="utf-8", newline="\n") as f:
        f.write(script)

    r = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--mount", "--", "bash", sp],
        capture_output=True, timeout=60,
    )
    return r.returncode == 0 and os.path.exists(real)
def versions_of(workdir: str) -> list[dict]:
    """这个任务目录的所有版本，从旧到新。

    ⚠️ 两种版本都要算：

        已冻结的     v1, v2, ...   （上一轮跑完冻下来的）
        正在进行的   current       （这一轮刚改的，还没冻结）

    实测（2026-11-03）一开始漏了 current，
    于是"刚改完还没冻结"的那一轮在列表里看不见 ——
    用户会以为"我明明改了，怎么没有这个版本"。

    返回 [{"n": 1, "path": "...", "time": ..., "files": [...], "current": bool}]
    """
    import datetime

    L = _layers(workdir)
    out: list[dict] = []

    for i, p in enumerate(_versions(workdir)):
        try:
            when = datetime.datetime.fromtimestamp(os.path.getmtime(p))
        except OSError:
            when = None
        out.append({
            "n": i + 1,
            "path": p,
            "time": when,
            "files": _list_files(p),
            "current": False,
        })

    # 正在进行的那一轮
    cur = L["current"]
    try:
        if os.path.isdir(cur) and os.listdir(cur):
            out.append({
                "n": len(out) + 1,
                "path": cur,
                "time": datetime.datetime.fromtimestamp(os.path.getmtime(cur)),
                "files": _list_files(cur),
                "current": True,
            })
    except OSError:
        pass

    return out


def _list_files(root: str) -> list[str]:
    """一个层里有哪些文件（相对路径）。"""
    out = []
    if not os.path.isdir(root):
        return out
    for dp, dns, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            out.append(os.path.relpath(p, root))
    return sorted(out)


def restore(workdir: str, version: int, paths: list[str] | None = None) -> dict:
    """把这个目录回退到某一层。

    version     回到第几层（1 起）
    paths       只回退这些文件（相对路径）；None = 全部

    返回 {"restored": [...], "failed": [...], "deleted": [...]}
    """
    versions = _versions(workdir)
    if not versions:
        return {"restored": [], "failed": [], "deleted": [], "error": "没有版本"}

    if version < 1 or version > len(versions):
        return {"restored": [], "failed": [], "deleted": [],
                "error": f"版本号超出范围（1..{len(versions)}）"}

    L = _layers(workdir)

    # 回退 = 把目标层及以下的文件，写回真机
    #
    # ⚠️ 做法：
    #    叠一层 overlay（lowerdir = 目标层:...:base，upperdir 用一个临时目录）
    #    读 merged → 那就是"回到那一层"的样子
    #    把要求的文件从 merged 拷回真机
    import tempfile

    target_layers = list(reversed(versions[:version])) + [L["base"]]
    lowerdir = ":".join(target_layers)

    tmp_root = tempfile.mkdtemp(prefix="dsh_restore_")
    upper = os.path.join(tmp_root, "upper")
    work = os.path.join(tmp_root, "work")
    merged = os.path.join(tmp_root, "merged")
    for d in (upper, work, merged):
        os.makedirs(d, exist_ok=True)

    script = (
        f"mount -t overlay overlay -o "
        f"lowerdir={lowerdir},upperdir={upper},workdir={work} {merged}\n"
        f"cd {merged}\n"
        f"find . -type f -printf '%P\\n'\n"
    )
    script_path = os.path.join(tmp_root, "ls.sh")
    with open(script_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(script)

    want = paths
    restored: list[str] = []
    failed: list[str] = []

    # 在 overlay 里读出文件，写回真机
    #
    # ⚠️ 不能直接拷 merged 里的文件 —— 那个挂载点只在 unshare 里可见。
    #    所以让 unshare 里的 bash 自己把文件写到真机路径。
    targets = want if want is not None else []
    if want is None:
        # 先拿文件清单
        r = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--mount", "--", "bash", script_path],
            capture_output=True, timeout=60,
        )
        listing = (r.stdout or b"").decode("utf-8", "replace")
        targets = [ln.strip() for ln in listing.splitlines() if ln.strip()]

    for rel in targets:
        real = os.path.join(workdir, rel)
        shadow = os.path.join(merged, rel)
        try:
            os.makedirs(os.path.dirname(real), exist_ok=True)
            # 在 unshare 里 cp
            cp_script = (
                f"mount -t overlay overlay -o "
                f"lowerdir={lowerdir},upperdir={upper},workdir={work} {merged}\n"
                f'cp -p "{shadow}" "{real}"\n'
            )
            cpp = os.path.join(tmp_root, "cp.sh")
            with open(cpp, "w", encoding="utf-8", newline="\n") as f:
                f.write(cp_script)
            rr = subprocess.run(
                ["unshare", "--user", "--map-root-user", "--mount", "--", "bash", cpp],
                capture_output=True, timeout=60,
            )
            if rr.returncode == 0:
                restored.append(rel)
            else:
                failed.append(rel)
        except OSError:
            failed.append(rel)

    import shutil as _sh

    _sh.rmtree(tmp_root, ignore_errors=True)

    return {"restored": restored, "failed": failed, "deleted": []}

def run_in_box(
    command: str,
    cwd: str,
    env: dict | None = None,
    timeout: int = 120,
    net: bool = False,
) -> subprocess.CompletedProcess | None:
    """在影子里跑一条命令。

    net     False  断网（默认）
            True   放行联网（用户授权之后）

    返回 CompletedProcess；这套不可用时返回 None。
    """
    if not available():
        return None

    ok, why = prepare(cwd)
    if not ok:
        return None

    L = _layers(cwd)
    script = build_script(cwd, command)

    # 脚本写到影子目录里（真机目录不能碰）
    script_path = os.path.join(L["root"], "_run.sh")
    try:
        with open(script_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(script)
        os.chmod(script_path, 0o755)
    except OSError:
        return None

    argv = [
        "unshare",
        "--user", "--map-root-user",
        "--mount",
    ]
    if not net:
        argv += ["--net"]

    argv += ["--", "bash", script_path]

    e = dict(env or os.environ)
    e["PYTHONIOENCODING"] = "utf-8"

    try:
        return subprocess.run(
            argv,
            cwd=L["root"],
            env=e,
            capture_output=True,
            timeout=timeout,
            text=False,
        )
    except subprocess.TimeoutExpired:
        raise
    except OSError:
        return None


# ══════════════════════════════════════════════════════
# 改了什么
# ══════════════════════════════════════════════════════

def _walk(root: str) -> dict[str, tuple[int, float]]:
    """{相对路径: (大小, mtime)}。用来比"改了没"。"""
    out: dict[str, tuple[int, float]] = {}
    if not os.path.isdir(root):
        return out
    for dp, dns, fns in os.walk(root):
        for fn in fns:
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, root)
            try:
                st = os.lstat(p)
                # whiteout 是字符设备，主次设备号都是 0
                if stat.S_ISCHR(st.st_mode):
                    try:
                        if (os.major(st.st_rdev), os.minor(st.st_rdev)) == WHITEOUT_DEV:
                            out[rel] = (-1, 0.0)   # 特殊标记：删了
                            continue
                    except AttributeError:
                        pass
                out[rel] = (st.st_size, st.st_mtime)
            except OSError:
                continue
    return out


def classify_changes(workdir: str) -> dict:
    """这次影子里改了什么。

    返回跟 Windows 版一样的结构：

        {
            "deletes":  [真机绝对路径...],
            "writes":   [(影子路径, 真机路径)...],
            "modifies": [(影子路径, 真机路径)...],
        }
    """
    L = _layers(workdir)
    real_root = os.path.abspath(workdir)

    # ⚠️ 改版之后"当前层"是最后一个版本目录，不再是固定的 upper。
    #    只看这一层就够 —— 更早的层是历史，不是这次的改动。
    versions = _versions(workdir)
    if not versions:
        return {"deletes": [], "writes": [], "modifies": []}
    upper = versions[-1]

    out: dict = {"deletes": [], "writes": [], "modifies": []}

    if not os.path.isdir(upper):
        return out

    seen: set[str] = set()

    for dp, dns, fns in os.walk(upper):
        for fn in fns:
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, upper)
            if rel in seen:
                continue
            seen.add(rel)

            real = os.path.join(real_root, rel)

            # ⚠️ shadow 要用 **upper 层**里的真实路径，不能用 merged。
            #
            #    merged 是 unshare 里的挂载点 —— 进程一退出，挂载没了，
            #    那个路径就指向一个空目录。
            #
            #    实测（2026-11-03）：
            #        classify_changes 返回 merged/a.txt → 不存在
            #        upper/a.txt                        → 存在，10 字节
            #
            #    结果就是"落地 0 个文件，2 个失败" —— 一个都没写回去。
            shadow = p

            try:
                st = os.lstat(p)
            except OSError:
                continue

            # whiteout：这个文件在影子里被删了
            #
            # ⚠️ 不要写 os.path.stat.S_ISCHR —— os.path 下面能摸到 stat，
            #    那是实现细节，不是公开接口。用 import stat 的那个。
            if stat.S_ISCHR(st.st_mode):
                try:
                    dev = (os.major(st.st_rdev), os.minor(st.st_rdev))
                except AttributeError:
                    dev = (0, 0)
                if dev == WHITEOUT_DEV:
                    out["deletes"].append(real)
                    continue

            if not os.path.exists(real):
                out["writes"].append((shadow, real))
            else:
                out["modifies"].append((shadow, real))

    return out


def has_changes(workdir: str) -> bool:
    c = classify_changes(workdir)
    return bool(c["deletes"] or c["writes"] or c["modifies"])
