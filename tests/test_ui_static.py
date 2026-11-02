"""静态检查：桌面端有没有"调用了但没定义"的方法。

⚠️ 为什么需要这个测试：

    2026-11-02 一天里犯了两次同样的错：
        b_unlock.clicked.connect(self._do_unlock)       ← _do_unlock 不存在
        self._show_action_error(path, data)             ← 不存在

    两个都是**运行时才炸**，而且都在 __init__ 里，
    结果就是**整个桌面端打不开**，连窗口都没有。

    py_compile 检查不出来（语法是对的）。
    跑 pytest 也检查不出来（没人 import 那个界面）。

    所以我们才需要这一条：扫源码，找出
        self.xxx(  有调用
        但没有  def xxx(
    的私有方法。

    它是静态的、秒级的、不需要 Qt 环境的。

⚠️ 2026-11-02 晚：MainWindow 拆成 mixin 之后，这个方法**必须跟着改**。

    原来只扫 desktop/app.py 一个文件，于是 mixin 里定义的方法
    它看不见 —— 报了一堆"_build_tasks_page 没定义"，其实在
    desktop/mixins/tasks_mixin.py 里。

    现在：把 app.py + 它继承的所有 mixin 当成**一个整体**来扫。
    MainWindow 是哪些 mixin 拼出来的，从 app.py 的 import 里自动认。
"""

from __future__ import annotations

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DESKTOP = os.path.join(ROOT, "desktop")
MIXINS = os.path.join(DESKTOP, "mixins")


def _scan_file(path: str) -> tuple[set, set, set]:
    """扫一个文件，返回 (定义的方法名, self.xxx 调用的, connect 里引用的)。"""
    if not os.path.exists(path):
        return set(), set(), set()
    tree = ast.parse(open(path, encoding="utf-8").read())

    defined = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    called = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            v = n.func.value
            if isinstance(v, ast.Name) and v.id == "self":
                called.add(n.func.attr)

    conn = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr == "connect":
                for a in n.args:
                    if isinstance(a, ast.Attribute):
                        v = a.value
                        if isinstance(v, ast.Name) and v.id == "self":
                            conn.add(a.attr)
    return defined, called, conn


def _mixin_files() -> list[str]:
    """desktop/mixins/ 下所有的 .py（不含 __init__）。"""
    if not os.path.isdir(MIXINS):
        return []
    out = []
    for fn in sorted(os.listdir(MIXINS)):
        if fn.endswith(".py") and fn != "__init__.py":
            out.append(os.path.join(MIXINS, fn))
    return out


def _missing_for(paths: list[str]) -> list[str]:
    """把这些文件当成一个整体，找"调用了但哪都没定义"的私有方法。"""
    defined: set[str] = set()
    called: set[str] = set()
    conn: set[str] = set()

    for p in paths:
        d, c, k = _scan_file(p)
        defined |= d
        called |= c
        conn |= k

    miss = {m for m in (called | conn) - defined if m.startswith("_")}
    return sorted(m for m in miss if not m.startswith("__"))


def test_desktop_app_no_undefined_methods():
    """MainWindow（含它的 mixin）里没有"调用了但没定义"的私有方法。

    这类错会直接让桌面端起不来 —— 连窗口都画不出来，
    因为 connect/call 都发生在 __init__ 里。
    """
    # app.py + 所有 mixin，当成一个整体
    paths = [os.path.join(DESKTOP, "app.py")] + _mixin_files()
    miss = _missing_for(paths)
    assert not miss, (
        f"MainWindow（app.py + mixins/）里有调用了但没定义的方法：{miss}\n"
        "这类错会让整个桌面端打不开（AttributeError 发生在 __init__ 里）。"
    )


def test_all_modules_exist():
    """app.py 里 import 的 mixin，文件得真的存在。

    拆成多文件之后多了一种新的坏法：
        app.py 写着 from mixins.foo import Foo
        但 mixins/foo.py 不存在（改名字忘了改 import）

    这种是 ImportError，桌面端也起不来 —— 一并挡掉。
    """
    app_py = os.path.join(DESKTOP, "app.py")
    if not os.path.exists(app_py):
        return

    src = open(app_py, encoding="utf-8").read()
    missing = []
    for m in re.finditer(r"^from (mixins\.\w+) import", src, re.M):
        mod = m.group(1)  # 比如 mixins.foo
        name = mod.split(".", 1)[1]
        if not os.path.exists(os.path.join(MIXINS, name + ".py")):
            missing.append(mod)

    assert not missing, f"app.py 引用了不存在的 mixin 模块：{missing}"


def test_launcher_no_undefined_methods():
    """desktop/launcher.py 同样检查一遍。"""
    miss = _missing_for([os.path.join(DESKTOP, "launcher.py")])
    assert not miss, f"desktop/launcher.py 里有调用了但没定义的方法：{miss}"
