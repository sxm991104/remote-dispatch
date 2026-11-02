"""开内核级网络隔离（WFP）。

⚠️ 用管理员权限跑：
      右键开始菜单 → 终端(管理员)
      cd D:\\Project\\dsh-bridge-oss
      C:\\Users\\19836\\AppData\\Roaming\\uv\\tools\\astrbot\\Scripts\\python.exe tools\\enable_wfp.py

## 为什么要这么绕

    SandMan.exe 在内存里维护一份配置，它会定期把内存那份写回
    Sandboxie.ini。所以：

        改文件 → SandMan 不知道 → 它一重写，改动就没了

    实测（2026-11-03 凌晨）踩了三次：
        写了 NetworkEnableWFP=y
        重启之后一看，那一行被删得干干净净

    正确顺序：
        一、关掉 SandMan（它在跑就会抢着写）
        二、改 ini
        三、启动 SandMan（让它读到新配置）

## 为什么 Python 而不是 .bat

    .bat 文件在 cmd 里按系统 ANSI 代码页（中文是 GBK）读。
    我写的时候存成 UTF-8，于是中文全变乱码，
    整个脚本解析失败 —— 用户看到一堆"不是内部或外部命令"。

    Python 脚本自己管编码，不会有这个问题。
"""

from __future__ import annotations

import datetime
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

INI = r"C:\Windows\Sandboxie.ini"
SBIEINI = r"C:\Program Files\Sandboxie-Plus\SbieIni.exe"
SANMAN = r"C:\Program Files\Sandboxie-Plus\SandMan.exe"


def is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    print("=" * 68)
    print("  开内核级网络隔离（WFP）")
    print("=" * 68)
    print()

    if sys.platform != "win32":
        print("  只在 Windows 上跑")
        return 1

    if not is_admin():
        print("  ❌ 需要管理员权限")
        print()
        print("     右键开始菜单 → 终端(管理员)，然后：")
        print(f"       cd {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}")
        print(f"       python tools\\enable_wfp.py")
        return 1

    print("  ✅ 管理员权限")

    if not os.path.exists(INI):
        print(f"  ❌ 找不到 {INI}")
        return 1

    # ── 备份 ──
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = f"{INI}.bak-wfp-{ts}"
    try:
        with open(INI, "rb") as f:
            data = f.read()
        with open(bak, "wb") as f:
            f.write(data)
        print(f"  ✅ 备份 → {bak}")
    except OSError as e:
        print(f"  ❌ 备份失败：{e}")
        return 1

    # ── 关 SandMan ──
    print()
    print("  正在关 SandMan（它在跑就会用内存那份覆盖配置）...")
    for name in ("SandMan.exe", "SbieCtrl.exe"):
        subprocess.run(
            ["taskkill", "/IM", name, "/F"],
            capture_output=True,
        )
    print("  ✅ 关了")

    import time

    time.sleep(2)

    # ── 用官方 CLI 设置 ──
    print()
    print("  设置 NetworkEnableWFP=y ...")
    r = subprocess.run(
        [SBIEINI, "set", "GlobalSettings", "NetworkEnableWFP", "y"],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    print(f"    退出码: {r.returncode}")

    # ── 确认 ──
    print()
    print("  检查有没有写进去：")
    try:
        if sys.platform == "win32":
            pass
    except Exception:  # noqa: BLE001
        pass

    # ⚠️ ini 是 UTF-16，必须按那个编码读
    body = data.decode("utf-16", errors="replace")
    # 重新读一遍（SbieIni 可能刚写过）
    try:
        with open(INI, "rb") as f:
            data2 = f.read()
        body = data2.decode("utf-16", errors="replace")
    except OSError:
        pass

    if "NetworkEnableWFP" in body:
        # 找到它所在的行
        for ln in body.splitlines():
            if "NetworkEnableWFP" in ln:
                print(f"    ✅ {ln.strip()}")
                break
    else:
        print("    ❌ 没写进去")
        print(f"    可以从备份恢复：{bak}")

    # ── 启动 SandMan ──
    print()
    print("  正在启动 SandMan ...")
    try:
        subprocess.Popen(
            [SANMAN],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("    ✅ 起来了")
    except OSError as e:
        print(f"    ⚠️ 起不来：{e}")

    print()
    print("=" * 68)
    print("  完成")
    print("=" * 68)
    print()
    print("  现在**重启计算机**，WFP 才会真正生效。")
    print()
    print("  重启之后回来跑这个确认：")
    print("      python tools\\harden_net.py")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
