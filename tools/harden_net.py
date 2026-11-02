"""硬核两步：给沙箱断网 + 开内核级 WFP。

⚠️ 这个脚本**必须用管理员权限跑**。

用法：
    右键 PowerShell → 以管理员身份运行
    cd D:\\Project\\dsh-bridge-oss
    python tools\\harden_net.py

    然后重启计算机（WFP 要重启才生效）

它做两件事：
    一、在 [RemoteDispatch] 沙箱里加 AllowNetworkAccess=n
         → 沙箱里跑的程序出不去网

    二、在 [GlobalSettings] 里加 NetworkEnableWFP=y
         → 断网从"用户态挂钩"升级到"内核级过滤"
         → 恶意程序绕不过

⚠️ 为什么第二个要用户自己跑，不由程序自动做

    它改的是 **Sandboxie 的全局配置**，影响所有沙箱，
    不只是我们的。而且要重启计算机。

    这类"动别人东西 + 要重启"的操作，不该由程序替用户决定。

⚠️ 改之前自动备份
    备份在 Sandboxie.ini 同目录，名字带时间戳。
"""

from __future__ import annotations

import datetime
import os
import re
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

INI = r"C:\Windows\Sandboxie.ini"

BOX = "RemoteDispatch"
WFP_SEC = "[GlobalSettings]"
WFP_KEY = "NetworkEnableWFP"
BOX_KEY = "AllowNetworkAccess"


def is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def detect_enc(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe"):
        return "utf-16"
    if raw.startswith(b"\xfe\xff"):
        return "utf-16"
    if raw.count(b"\x00") > len(raw) // 4:
        return "utf-16-le"
    return "utf-8"


def backup() -> str:
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = INI + f".bak-hardennet-{ts}"
    shutil.copy2(INI, dst)
    return dst


def set_key(body: str, section: str, key: str, val: str) -> tuple[str, str]:
    """在某个段里设一个键。返回 (新全文, 说明)。"""
    m = re.search(
        rf"^\[{re.escape(section.strip('[]'))}\]([\s\S]*?)(?=^\[|\Z)",
        body, re.M,
    )
    if not m:
        return body, f"没有 [{section.strip('[]')}] 段"

    seg = m.group(1)
    lines = seg.splitlines()

    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.lower().startswith(key.lower() + "="):
            old = s.split("=", 1)[1].strip()
            if old.lower() == val.lower():
                return body, f"已经是 {key}={val}"
            lines[i] = f"{key}={val}"
            new_seg = "\n".join(lines)
            body = body[: m.start(1)] + new_seg + body[m.end(1):]
            return body, f"{key}: {old} → {val}"

    # 没有这个键 → 加在段开头
    lines.insert(0, f"{key}={val}")
    new_seg = "\n".join(lines)
    body = body[: m.start(1)] + new_seg + body[m.end(1):]
    return body, f"加上 {key}={val}"


def main() -> int:
    print("=" * 70)
    print("  网络硬隔离")
    print("=" * 70)
    print()

    if sys.platform != "win32":
        print("  ❌ 这个脚本只在 Windows 上跑")
        print("     Linux 上的做法是 bwrap --unshare-net（还没接）")
        return 1

    if not os.path.exists(INI):
        print(f"  ❌ 找不到 {INI}")
        return 1

    if not is_admin():
        print("  ❌ 需要管理员权限。")
        print()
        print("     右键开始菜单 → 终端(管理员)，然后：")
        print(f"       cd {ROOT}")
        print("       python tools\\harden_net.py")
        return 1

    print(f"  ✅ 有管理员权限")
    print(f"  配置文件: {INI}")

    raw = open(INI, "rb").read()
    enc = detect_enc(raw)
    print(f"  编码: {enc}")
    print()

    body = raw.decode(enc, errors="replace")

    # ── 备份 ──
    dst = backup()
    print(f"  已备份 → {dst}")
    print()

    changed = False

    # ── 一、沙箱断网 ──
    print("-" * 70)
    print("  一、给沙箱断网")
    print("-" * 70)

    if f"[{BOX}]" not in body:
        print(f"  ⚠️ 沙箱 [{BOX}] 还没建 —— 先在桌面端点一次「启动」")
    else:
        body, msg = set_key(body, BOX, BOX_KEY, "n")
        print(f"  {msg}")
        changed = True

    print()

    # ── 二、开 WFP ──
    print("-" * 70)
    print("  二、开内核级过滤（WFP）")
    print("-" * 70)
    print()
    print("  ⚠️ 这一步改的是 **Sandboxie 的全局配置**，")
    print("     影响所有沙箱，不只是我们的。")
    print("     而且**必须重启计算机**才生效。")
    print()

    ans = input("  要做吗？(y/N) ").strip().lower()
    if ans == "y":
        body, msg = set_key(body, WFP_SEC, WFP_KEY, "y")
        print(f"  {msg}")
        changed = True
    else:
        print("  跳过。现在还是软隔离 —— 恶意程序能绕过。")

    print()

    # ── 写回 ──
    if changed:
        try:
            with open(INI, "w", encoding=enc) as f:
                f.write(body)
            print(f"  ✅ 已写入（按 {enc}）")
        except OSError as e:
            print(f"  ❌ 写失败：{e}")
            print(f"     可以从备份恢复：{dst}")
            return 1
    else:
        print("  什么都没改。")

    print()
    print("=" * 70)
    print("  现在的状态")
    print("=" * 70)

    from guard import netguard

    s = netguard.status()
    for k, v in s.items():
        print(f"  {k}: {v}")

    print()
    if s.get("wfp"):
        print("  ⚠️ WFP 要**重启计算机**才生效。")
        print("     重启之后，沙箱里的程序就真的出不去了。")
    else:
        print("  提示：开着 WFP 才是硬隔离。现在只有软隔离。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
