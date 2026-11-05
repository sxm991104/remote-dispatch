"""记下任务改了哪些文件，之后能查历史版本。

## 这个模块干什么

    任务跑完，用户总得问一句："你刚才改了啥？"
    更狠的时候："你把它改坏了，改之前什么样？"

    这两件事都需要**知道"改了哪些文件"**。

    而"改之前长什么样"**不用我们存** ——
    Windows 的系统卷影副本里本来就有，guard/history.py 能读出来。

    所以这里只做一件事：
        **在工具真正动手之前，把"它要碰哪些路径"记下来。**

## ⚠️ 为什么记"要碰的路径"而不是"改完再扫"

    改完再扫要扫一大片目录（甚至整个盘），慢，而且分不清
    哪些是任务改的、哪些是别的程序改的。

    而 execute() 拿到的参数里本来就写着路径：
        write_file   {"path": "a.py", "content": "..."}
        edit_file    {"path": "a.py", "old": ..., "new": ...}
        run_command  {"command": "..."}     ← 这个要看命令里提到哪些路径

    动手前记下来，最准。

## ⚠️ 只记，不动任何东西

    这个模块**不写文件、不删文件、不回退**。
    用户要退回去，去 Windows 的「以前的版本」自己找 ——
    那样最可控（用户自己看着办，出错不怪我们）。

    （2026-11-05 老爸定的：我们只接插件、给对比，不帮用户回退。）
"""

from __future__ import annotations

import json
import os
import re
import time


def _ledger_path() -> str:
    from tools import userdirs

    return userdirs.config_file("change_ledger.jsonl")


# 哪些工具会改东西
#
# ⚠️ read_file / list_dir / search_files 只读，不用记。
#    记了也是噪音，用户查历史版本时会看到一堆"其实没动"的条目。
MUTATING = {"write_file", "edit_file", "run_command"}


def paths_in_args(name: str, args: dict) -> list[str]:
    """这次工具调用会碰哪些路径。

    返回绝对路径列表（去重、去掉不存在的父目录也能留着的部分）。
    """
    out: list[str] = []

    if name in ("write_file", "edit_file"):
        p = str(args.get("path") or "").strip()
        if p:
            out.append(os.path.abspath(p))
        return out

    if name == "run_command":
        return _paths_in_command(str(args.get("command") or ""))

    return out


def _paths_in_command(cmd: str) -> list[str]:
    """从一条命令里抠出它会碰哪些路径。

    ## ⚠️ 这是启发式，不可能完美

        `rm -rf *` 碰的东西，从字面上看不出来（* 是 shell 展开的）。
        `python script.py` 碰的东西，在 script.py 里面。

        能抠出来的：
            · 命令里出现的绝对路径（C:\\... / D:/... / /usr/...）
            · 相对路径（./xxx  xxx/yyy）
            · 重定向目标（> file  >> file）

        抠不出来的就记"这条命令本身"，让用户知道"这个时刻跑过命令"，
        真要看细节，去「以前的版本」按时间找。

        宁可粗一点，也别假装精确 ——
        精确的假信息比模糊的真信息更害人。
    """
    out: list[str] = []
    c = cmd or ""

    # Windows 绝对路径：字母:\...
    for m in re.finditer(r"[A-Za-z]:[\\/][^\s\"'<>|&;]*", c):
        p = m.group(0).rstrip("\\/")
        if len(p) > 2:
            out.append(os.path.abspath(p))

    # Unix 绝对路径（只取看起来像项目目录的，别把 /bin/ls 也记上）
    for m in re.finditer(r"(?<![\w:])(/(?:home|root|tmp|opt|var|usr/local)[^\s\"'<>|&;]*)", c):
        out.append(m.group(1))

    # 相对路径：./xxx  ../xxx  dir/file.ext
    for m in re.finditer(r"(?<![\w./-])(\.{0,2}/[^\s\"'<>|&;]+)", c):
        out.append(os.path.abspath(m.group(1)))

    # 重定向目标
    for m in re.finditer(r">>?\s*([^\s\"'<>|&;]+)", c):
        t = m.group(1).strip()
        if t and t not in ("/dev/null", "NUL", "nul"):
            out.append(os.path.abspath(t))

    # 去重保序
    seen = set()
    uniq = []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq[:50]      # 一条命令最多记 50 个（防止 `find /` 那种爆掉）


def record(task: str, name: str, args: dict) -> None:
    """记一条改动。失败就算了 —— 记账不能影响正事。"""
    if name not in MUTATING:
        return

    try:
        paths = paths_in_args(name, args)
        if not paths:
            return

        row = {
            "ts": time.time(),
            "task": str(task or ""),
            "tool": name,
            "paths": paths,
        }

        # run_command 额外记命令本身（路径抠不全的时候还有个线索）
        if name == "run_command":
            row["cmd"] = str(args.get("command") or "")[:500]

        p = _ledger_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

        # 账本超过 5MB 就砍掉前面一半
        try:
            if os.path.getsize(p) > 5 * 1024 * 1024:
                with open(p, encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                with open(p, "w", encoding="utf-8") as f:
                    f.writelines(lines[len(lines) // 2:])
        except OSError:
            pass
    except Exception:  # noqa: BLE001
        pass


def read_ledger(limit: int = 500) -> list[dict]:
    """读最近的改动记录（新的在前）。"""
    p = _ledger_path()
    if not os.path.exists(p):
        return []

    rows: list[dict] = []
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict):
                    rows.append(d)
    except OSError:
        return []

    rows.sort(key=lambda x: float(x.get("ts") or 0), reverse=True)
    return rows[:limit]


def files_of(task: str = "", limit: int = 300) -> list[str]:
    """这些改动一共碰过哪些文件（去重，最近的在前）。

    task 留空 = 所有任务。
    """
    seen: dict[str, float] = {}
    for row in read_ledger(limit=5000):
        if task and str(row.get("task") or "") != task:
            continue
        ts = float(row.get("ts") or 0)
        for p in (row.get("paths") or []):
            p = str(p)
            if not p:
                continue
            if p not in seen or ts > seen[p]:
                seen[p] = ts

    return [p for p, _t in sorted(seen.items(), key=lambda x: -x[1])][:limit]


def versions(path: str) -> list[dict]:
    """这个文件有哪些历史版本。

    直接问系统卷影副本（guard/history.py），我们自己的账本在这儿没用 ——
    账本只知道"碰过"，不知道"以前长什么样"。

    返回 [{"copy": 5, "time": "..."}]，新的在前。
    """
    try:
        from guard import history

        vs = history.find_versions(path)
        return [
            {"copy": int(v.get("copy") or 0),
             "time": v.get("time").strftime("%Y-%m-%d %H:%M:%S")
             if hasattr(v.get("time"), "strftime") else str(v.get("time") or "")}
            for v in vs
        ]
    except Exception:  # noqa: BLE001
        return []


def diff_with(path: str, copy: int) -> str:
    """当前内容 vs 某个历史版本的差异。"""
    try:
        from guard import history

        old = history.read_version(path, copy)
        if old is None:
            return "读不到那个版本（可能太大，或者那个快照里没有这个文件）"

        if not os.path.exists(path):
            return "这个文件现在不存在了（被删了？）"

        with open(path, "rb") as f:
            new = f.read()

        return history.diff_text(old, new)
    except Exception as e:  # noqa: BLE001
        return f"比不了：{type(e).__name__}: {e}"
