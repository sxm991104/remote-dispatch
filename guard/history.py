"""读写系统卷影副本里的历史版本。**我们自己不存任何东西。**

## 实测出来的关键事实（2026-11-03）

    ✅ 系统保护能开
        Enable-ComputerRestore -Drive "C:\\","D:\\"
        （必须同时带系统盘，否则报"驱动器列表中包括系统驱动器"）

    ✅ 快照能建
        Checkpoint-Computer
        默认 24 小时只能建一次 → 注册表
        HKLM\\...\\SystemRestore\\SystemRestorePointCreationFrequency = 0 可解除

    ✅ 普通用户能看到「以前的版本」里的内容（用户亲眼确认）

    ❌ WMI 查快照列表（Get-CimInstance Win32_ShadowCopy）
        普通用户报 0x80041014（要管理员）
        → 所以**不用它**

    ❌ Python 的 open() 打不开 \\\\?\\GLOBALROOT 路径
        → 所以**走 PowerShell 的 System.IO**

    ✅ Test-Path 对设备路径也不行
        → 用 [System.IO.File]::GetLastWriteTime 判断存在性
           存在返回真实时间，不存在返回 1601-01-01

## 结论

    探测影子卷    暴力扫 Copy1..CopyN
    判断有没有    GetLastWriteTime != 1601-01-01
    读内容       ReadAllBytes + base64 传回

    全程普通权限，全程不存东西。
"""

from __future__ import annotations

import base64
import datetime
import os
import re
import subprocess
import sys

SHADOW = r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy"

# Windows 的时间零点。GetLastWriteTime 找不到文件时返回它。
EPOCH_1601 = datetime.datetime(1601, 1, 1)

# 一次最多探多少号影子卷。
#
# ⚠️ 影子卷编号不是连续的（删掉一个，编号就断了）。
#    实测：Copy5 和 Copy7 存在，Copy6 是 C 盘的。
#    所以必须扫一段，不能"找到就停"。
MAX_COPY = 64

# 单个文件最多读多少字节（防止一个几百 MB 的文件把内存吃了）
MAX_READ = 32 * 1024 * 1024


def _ps(script: str, timeout: int = 60) -> str:
    """跑 PowerShell，返回 stdout。"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=timeout,
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )
    return (r.stdout or "").strip()


def available() -> tuple[bool, str]:
    """这套能不能用。"""
    if sys.platform != "win32":
        return False, "只有 Windows 有卷影副本（Linux 上另有实现，暂未做）"

    out = _ps("$PSVersionTable.PSVersion.Major", timeout=30)
    if not out.isdigit():
        return False, "PowerShell 不可用"

    return True, "可用（系统卷影副本）"


def _to_rel(path: str) -> tuple[str, str]:
    """把 D:\\a\\b 拆成 ("D:", "\\a\\b")。"""
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    return drive.upper(), rest


def find_versions(path: str, max_copies: int = MAX_COPY) -> list[dict]:
    """这个文件在哪些快照里存在。

    返回 [{"copy": 5, "time": datetime, "device": "..."}]，按时间排序。
    """
    if not os.path.exists(path):
        return []

    _drive, rest = _to_rel(path)
    if not rest:
        return []

    # 一次性把 1..N 都查了（一次 PowerShell 调用，不循环 N 次）
    lines = []
    for n in range(1, max_copies + 1):
        p = f"{SHADOW}{n}{rest}"
        lines.append(
            f"$p{n} = '{p}'; "
            f"try {{ "
            f"  $t = [System.IO.File]::GetLastWriteTime($p{n}); "
            f"  if ($t.Year -gt 1601) {{ Write-Output ('{n}|' + $t.ToString('yyyy-MM-dd HH:mm:ss')) }} "
            f"}} catch {{ }}"
        )

    out = _ps("; ".join(lines), timeout=120)

    result = []
    for ln in out.splitlines():
        if "|" not in ln:
            continue
        n_s, t_s = ln.split("|", 1)
        try:
            n = int(n_s.strip())
            when = datetime.datetime.strptime(t_s.strip(), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        result.append({
            "copy": n,
            "time": when,
            "device": f"{SHADOW}{n}",
        })

    result.sort(key=lambda x: x["time"])
    return result


def read_version(path: str, copy: int, max_bytes: int = MAX_READ) -> bytes | None:
    """从第 copy 号快照里读这个文件。读不到返回 None。"""
    _drive, rest = _to_rel(path)
    if not rest:
        return None

    p = f"{SHADOW}{copy}{rest}"

    script = (
        f"$p = '{p}'; "
        f"try {{ "
        f"  $b = [System.IO.File]::ReadAllBytes($p); "
        f"  if ($b.Length -gt {max_bytes}) {{ $b = $b[0..{max_bytes - 1}] }}; "
        f"  [Convert]::ToBase64String($b) "
        f"}} catch {{ Write-Output 'ERR' }}"
    )

    out = _ps(script, timeout=180)
    if not out or out == "ERR":
        return None

    try:
        return base64.b64decode(out)
    except Exception:  # noqa: BLE001
        return None


def diff_text(old: bytes, new: bytes) -> str:
    """把两个版本的文本差异算出来。给用户看"改了什么"。"""
    import difflib

    try:
        a = old.decode("utf-8", "replace").splitlines()
        b = new.decode("utf-8", "replace").splitlines()
    except Exception:  # noqa: BLE001
        return "（二进制文件，没法按行比）"

    d = list(difflib.unified_diff(a, b, "旧", "新", lineterm="", n=2))
    if not d:
        return "（两个版本内容一样）"

    # 太长的截断
    if len(d) > 200:
        d = d[:200] + ["…（后面还有很多）"]
    return "\n".join(d)
