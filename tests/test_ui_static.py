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
"""

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _scan(rel: str) -> tuple[set, set, set]:
    """返回 (定义的方法名, self.xxx 调用的, connect 里引用的)。"""
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    if not os.path.exists(p):
        return set(), set(), set()
    tree = ast.parse(open(p, encoding="utf-8").read())

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


def _missing(rel: str) -> list[str]:
    defined, called, conn = _scan(rel)
    miss = {m for m in (called | conn) - defined if m.startswith("_")}
    return sorted(m for m in miss if not m.startswith("__"))


def test_desktop_app_no_undefined_methods():
    """desktop/app.py 里没有"调用了但没定义"的私有方法。

    这类错会直接让桌面端起不来 —— 连接窗口都画不出来，
    因为 connect/call 都发生在 __init__ 里。
    """
    miss = _missing("desktop/app.py")
    assert not miss, (
        f"desktop/app.py 里有调用了但没定义的方法：{miss}\n"
        "这类错会让整个桌面端打不开（AttributeError 发生在 __init__ 里）。"
    )


def test_launcher_no_undefined_methods():
    """desktop/launcher.py 同样检查一遍。"""
    miss = _missing("desktop/launcher.py")
    assert not miss, f"desktop/launcher.py 里有调用了但没定义的方法：{miss}"
