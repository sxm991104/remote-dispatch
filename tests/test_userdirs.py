"""配置目录：各平台该放哪儿，以及两份实现必须一致。

⚠️ 背景

    原来全项目 18 处这样写：

        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        os.path.join(base, "dsh-bridge", "xxx.json")

    Windows 上对。Linux 上 APPDATA 不存在 → 退到 ~ →

        ~/dsh-bridge/xxx.json      ← 散落在家目录根下

    实测（WSL2 Ubuntu 24.04）：
        token 生成在 /root/dsh-bridge/api_token.txt
        而 status.sh 去 /root/.config/dsh-bridge/ 找 → 找不到

    现在统一成 tools/userdirs.py。

    ⚠️ 但插件（plugins/）不能 import 它 ——
        插件被 AstrBot 加载，AstrBot 只把插件目录加进 sys.path，
        不会把我们仓库根加进去。

        所以插件那边单独写了 plugins/_dsh_paths.py。

        两份代码，必须给出一样的结果。下面第一条测试就是查这个。
"""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _load_plugin_paths():
    """把 plugins/_dsh_paths.py 当作独立模块加载（模拟 AstrBot 的加载方式）。"""
    p = os.path.join(ROOT, "plugins", "_dsh_paths.py")
    spec = importlib.util.spec_from_file_location("_plugin_dsh_paths", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_plugin_and_core_agree(monkeypatch):
    """插件的 _dsh_paths 和项目的 tools.userdirs 给出同一个目录。

    这两份是**重复实现**（没法共享），很容易改一边忘一边。
    这条测试就是那条"别忘了"的绳子。
    """
    from tools import userdirs

    pp = _load_plugin_paths()

    # 在几种环境组合下都比一遍
    cases = [
        {"APPDATA": r"C:\Users\x\AppData\Roaming"},
        {"XDG_CONFIG_HOME": "/home/x/.config"},
        {"XDG_CONFIG_HOME": ""},
        {},
    ]
    for case in cases:
        for k in ("APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"):
            if k in case:
                monkeypatch.setenv(k, case[k])
            else:
                monkeypatch.delenv(k, raising=False)

        a = userdirs.config_dir()
        b = pp.config_dir()
        assert os.path.normcase(a) == os.path.normcase(b), (
            f"环境 {case} 下两份实现不一致：\n"
            f"  tools/userdirs   → {a}\n"
            f"  plugins/_dsh_paths → {b}"
        )


def test_windows_uses_appdata(monkeypatch):
    """Windows 上放 %APPDATA%\\dsh-bridge。"""
    from tools import userdirs

    monkeypatch.setenv("APPDATA", r"C:\Users\someone\AppData\Roaming")
    d = userdirs.config_dir()

    assert "dsh-bridge" in d
    # 在 Windows 上跑就等于 %APPDATA%\dsh-bridge
    if sys.platform == "win32":
        assert d == os.path.join(
            r"C:\Users\someone\AppData\Roaming", "dsh-bridge"
        )


def test_posix_uses_xdg(monkeypatch):
    """Linux 上放 $XDG_CONFIG_HOME/dsh-bridge（默认 ~/.config）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    from tools import userdirs

    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdgtest")
    d = userdirs.config_dir()
    assert d == os.path.join("/tmp/xdgtest", "dsh-bridge"), d

    # 没设 XDG_CONFIG_HOME 就退到 ~/.config，**不是**在家目录根下
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    d2 = userdirs.config_dir()
    home = os.path.expanduser("~")
    assert d2 == os.path.join(home, ".config", "dsh-bridge"), d2
    assert d2 != os.path.join(home, "dsh-bridge"), (
        "又跑到家目录根下了 —— 这正是要修的那个问题"
    )


def test_macos_uses_application_support(monkeypatch):
    """macOS 上放 ~/Library/Application Support/dsh-bridge。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    from tools import userdirs

    d = userdirs.config_dir()
    parts = d.replace("\\", "/")
    assert "Library/Application Support/dsh-bridge" in parts, d


def test_config_file_joins():
    """config_file 就是 config_dir + 文件名。"""
    from tools import userdirs

    d = userdirs.config_dir()
    f = userdirs.config_file("guard.json")
    assert f == os.path.join(d, "guard.json")


def test_no_appdata_fallback_to_home_root():
    """全项目不该再有「APPDATA 不存在就退回家目录根」的写法。

    ⚠️ 这条是防回归的。

        原来有 18 处 `os.environ.get("APPDATA") or os.path.expanduser("~")`，
        换到 Linux 上配置就散落在 ~/dsh-bridge/。

        现在应该全部走 tools/userdirs 或 plugins/_dsh_paths。
        这个测试扫源码，发现那种写法就报错。
    """
    import re

    bad = []
    pat = re.compile(r'os\.environ\.get\(["\']APPDATA["\']\)\s*or\s*os\.path\.expanduser')

    # 这两个文件自己就是在处理这件事，跳过
    SKIP = {
        os.path.join("tools", "userdirs.py"),
        os.path.join("plugins", "_dsh_paths.py"),
    }

    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [
            d for d in dns
            if d not in ("__pycache__", ".git", "state", "workspaces", "tests")
        ]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, ROOT)
            if rel in SKIP:
                continue
            try:
                src = open(full, encoding="utf-8").read()
            except OSError:
                continue
            lines = src.splitlines()
            for i, ln in enumerate(lines, 1):
                if not pat.search(ln):
                    continue
                # ⚠️ 有一种是合法的：整个函数开头就有平台判断。
                #
                #    比如 desktop/paths.py 的 startup_dir()：
                #        if sys.platform != "win32":
                #            return ""
                #        base = os.environ.get("APPDATA") or ...
                #
                #    这个只在 Windows 上会执行到"算路径"那一步，
                #    非 Windows 直接返回空串。所以它没问题。
                #
                #    判断方法：往上找 20 行，看有没有 sys.platform 判断
                #    或者 os.name 判断，并且在同一段代码里。
                # 往上找这个函数里有没有平台判断。
                # 先找到它属于哪个 def，再看那个 def 到这里的范围。
                fn_start = 0
                for j in range(i - 1, -1, -1):
                    if lines[j].startswith("def ") or lines[j].startswith("    def "):
                        fn_start = j
                        break
                protected = any(
                    ("sys.platform" in lines[j] or "os.name ==" in lines[j])
                    for j in range(fn_start, i)
                )
                if protected:
                    continue
                bad.append(f"{rel}:{i}  {ln.strip()[:80]}")

    assert not bad, (
        "这些地方还是「APPDATA 不存在就退回家目录」的老写法，\n"
        "换到 Linux 上配置会散落在 ~/dsh-bridge/：\n  "
        + "\n  ".join(bad)
        + "\n\n改成 tools/userdirs.config_file(...)（插件用 plugins/_dsh_paths）。"
    )
