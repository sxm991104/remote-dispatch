"""第 2 条：网络隔离。

## 现在的问题

    沙箱能上网。

    词表里能拦住 `curl`、`wget`、`Invoke-WebRequest` 这些**命令**，
    但拦不住：

        python -c "import requests; requests.post(...)"
        node -e "fetch(...)"
        任何自己实现的 HTTP 客户端

    词表挡的是**用法**，挡不住**能力**。

## 解法：在沙箱层面断网

    沙箱里跑的任何东西，不管用什么语言、什么库，
    一律出不去 —— 这跟词表是两回事。

## ⚠️ Sandboxie 有两个档，差别很大

    一、用户模式挂钩（默认）
        AllowNetworkAccess=n
        只用用户态 hook。官方原话：
            "由于用户模式挂钩可能会被恶意应用绕过或禁用，
             因此无法保证强制实施。"
        也就是：**恶意程序能绕。**

    二、内核模式 WFP
        [GlobalSettings] 里 NetworkEnableWFP=y + 重启
        内核级过滤，同时管入站和出站，绕不过。

## 所以这个模块的立场

    「写一个绕过就能破的断网，比不写更危险 —— 它给的是假的安全感。」

    所以：
        · 沙箱里**总是**写 AllowNetworkAccess=n
          （至少挡掉不走心的绕过）
        · 但**先查** WFP 开没开
        · 没开就明确告诉用户："现在是软隔离，想要硬隔离得开 WFP + 重启"
        · **不替用户改全局配置** —— 那是越界
"""

from __future__ import annotations

import os
import re
import sys

# ── 全局设置里那个开关的名字 ──
WFP_SECTION = "[GlobalSettings]"
WFP_KEY = "NetworkEnableWFP"

# 沙箱里断网的那一行
BOX_KEY = "AllowNetworkAccess"
BOX_VAL = "n"


def _ini_path() -> str:
    from guard import sandbox as _sb

    return _sb._ini_path()


def _read() -> tuple[str, str]:
    """(文件全文, 编码)。读不到返回 ("", "")。"""
    from guard import sandbox as _sb

    p = _ini_path()
    if not p or not os.path.exists(p):
        return "", ""
    enc = _sb._detect_ini_encoding(p)
    try:
        with open(p, "r", encoding=enc, errors="replace") as f:
            return f.read(), enc
    except OSError:
        return "", ""


def _write(body: str, enc: str) -> bool:
    p = _ini_path()
    if not p:
        return False
    try:
        # ⚠️ 必须按原编码写回。往 UTF-16 的文件里写 UTF-8 字节，
        #    Sandboxie 读不懂，配置静默失效（这个坑在 ensure_box 里踩过）。
        with open(p, "w", encoding=enc) as f:
            f.write(body)
        return True
    except (OSError, PermissionError):
        return False


def wfp_enabled() -> tuple[bool, str]:
    """内核级过滤开着没。

    返回 (开着没, 说明)。
    """
    body, _enc = _read()
    if not body:
        return False, "读不到 Sandboxie.ini"

    # 在 [GlobalSettings] 段里找 NetworkEnableWFP=y
    m = re.search(
        r"^\[GlobalSettings\]([\s\S]*?)(?=^\[|\Z)",
        body, re.M,
    )
    if not m:
        return False, "没有 [GlobalSettings] 段"

    seg = m.group(1)
    for ln in seg.splitlines():
        s = ln.strip()
        if s.lower().startswith(WFP_KEY.lower() + "="):
            val = s.split("=", 1)[1].strip().lower()
            if val in ("y", "yes", "true", "1"):
                return True, "WFP 已开（内核级，绕不过）"
            return False, "WFP 关着（现在是软隔离，能绕）"

    return False, f"[GlobalSettings] 里没有 {WFP_KEY}"


def box_has_network_block() -> bool:
    """我们的沙箱里已经写了断网没有。"""
    from guard import sandbox as _sb

    body, _enc = _read()
    if not body:
        return False

    m = re.search(
        rf"^\[{re.escape(_sb.BOX_NAME)}\]([\s\S]*?)(?=^\[|\Z)",
        body, re.M,
    )
    if not m:
        return False

    for ln in m.group(1).splitlines():
        s = ln.strip()
        if s.lower().startswith(BOX_KEY.lower() + "="):
            return s.split("=", 1)[1].strip().lower() in ("n", "no", "false", "0")
    return False


def apply_to_box() -> tuple[bool, str]:
    """在我们自己的沙箱里断网。

    ⚠️ 只碰 [RemoteDispatch] 这一段，不碰 [GlobalSettings]。
        改别人的全局配置是越界 —— 而且那要重启才生效，
        不该由我们替用户决定。

    返回 (成功没, 说明)。
    """
    from guard import sandbox as _sb

    body, enc = _read()
    if not body:
        return False, "读不到 Sandboxie.ini（要管理员权限）"

    sec = re.search(
        rf"^\[{re.escape(_sb.BOX_NAME)}\]([\s\S]*?)(?=^\[|\Z)",
        body, re.M,
    )
    if not sec:
        return False, f"沙箱 [{_sb.BOX_NAME}] 还没建"

    seg = sec.group(1)
    lines = seg.splitlines()

    # 已经有这一行了，看值对不对
    found = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.lower().startswith(BOX_KEY.lower() + "="):
            found = True
            if s.split("=", 1)[1].strip().lower() != BOX_VAL:
                lines[i] = f"{BOX_KEY}={BOX_VAL}"
            break

    if not found:
        lines.insert(0, f"{BOX_KEY}={BOX_VAL}")

    new_seg = "\n".join(lines)
    body = body[: sec.start(1)] + new_seg + body[sec.end(1):]

    if not _write(body, enc):
        return False, "写不进去（要管理员权限）"

    wfp, why = wfp_enabled()
    if wfp:
        return True, f"沙箱已断网（{why}）"
    return True, (
        "沙箱已断网，但**现在是软隔离**："
        "只靠用户态挂钩，恶意程序能绕。"
        f"想要硬隔离，在 Sandboxie.ini 的 [GlobalSettings] 里加 "
        f"{WFP_KEY}=y 然后重启。"
    )


def status() -> dict:
    """一眼看清现在什么状况。给界面和日志用。"""
    from guard import sandbox as _sb

    if sys.platform != "win32":
        return {
            "platform": "non-windows",
            "box_block": False,
            "wfp": False,
            "detail": "Linux/macOS 上没有 Sandboxie，网络隔离要靠 bwrap --unshare-net（还没接）",
        }

    wfp, why = wfp_enabled()
    blk = box_has_network_block()

    if blk and wfp:
        detail = "硬隔离：沙箱里出不去网，内核级。"
    elif blk and not wfp:
        detail = (
            "软隔离：沙箱里写了断网，但没开 WFP，"
            "恶意程序能绕。"
        )
    elif not blk:
        detail = "沙箱**没断网** —— 网络侧的防护只剩词表。"
    else:
        detail = why

    return {
        "platform": "windows",
        "box_block": blk,
        "wfp": wfp,
        "detail": detail,
    }
