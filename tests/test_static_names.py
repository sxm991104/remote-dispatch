"""扫：文件里用了但既没定义、也没 import 的名字。

## 为什么需要这条

    2026-11-02 拆 desktop/app.py 的时候，连着犯了两次同样的错：

        第一次  mixin 里用了 QWidget，但文件没 import PySide6
        第二次  mixin 里用了 ServiceCard，但没 import widgets 里的控件

    两次都是：
        py_compile 过（语法对）
        test_ui_static 过（只看方法名）
        **窗口一开就 NameError**

    而同一天早些时候，_do_unlock 那次也是这个模式 ——
    静态能查的东西，别靠跑一遍碰运气。

## 怎么查

    ast 遍历，收集：
        · 所有 import 进来的名字
        · 所有 def/class 定义的名字
        · 所有赋值的名字（变量、for 循环变量、with as...）
        · 函数参数名
        · builtins

    然后看每个 Name 节点的 id 在不在上面那张表里。
    不在 → 报出来。

    ⚠️ 这是近似判断，不是完整的符号解析（不做作用域分析）。
        所以只用来抓"整个文件里根本没出现过这个名字"这种明显的错 ——
        那正好就是上面那三次。
"""

from __future__ import annotations

import ast
import builtins
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DESKTOP = os.path.join(ROOT, "desktop")


def _collect_names(tree: ast.AST) -> set[str]:
    """这个文件里"有定义"的名字（import / def / class / 赋值 / 参数）。"""
    # ⚠️ builtins 里没有 __file__ / __name__ / __doc__ 这些 ——
    #    它们是模块级的内置名字，ast 里会当成 Name 节点出现。
    #    第一版没加白名单，于是 __file__ 被误报成"没定义"。
    names: set[str] = set(dir(builtins)) | {
        "__file__", "__name__", "__doc__", "__package__", "__spec__",
        "__loader__", "__builtins__", "__annotations__", "__dict__",
    }

    for node in ast.walk(tree):
        # import x / import x.y as z
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        # from x import y as z
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "*":
                    continue
                names.add(a.asname or a.name)
        # def f(...) / class C
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        # 参数
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        # 赋值目标：x = ... / x, y = ... / for x in ... / with ... as x
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        # except E as e
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        # global x / nonlocal x
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            for n in node.names:
                names.add(n)

    return names


def _undefined_names(path: str) -> list[tuple[int, str]]:
    """返回 [(行号, 名字)]：用了但文件里哪都没定义的。"""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)

    defined = _collect_names(tree)

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in defined:
                out.append((node.lineno, node.id))

    # 去重（同一个名字可能用了很多次），按名字排
    seen = set()
    uniq = []
    for ln, name in sorted(out):
        if name in seen:
            continue
        seen.add(name)
        uniq.append((ln, name))
    return uniq


def _all_desktop_py() -> list[str]:
    out = []
    for dp, dns, fns in os.walk(DESKTOP):
        dns[:] = [d for d in dns if d != "__pycache__"]
        for fn in sorted(fns):
            if fn.endswith(".py"):
                out.append(os.path.join(dp, fn))
    return out


def test_desktop_files_have_no_undefined_names():
    """desktop/ 下每个文件都不该出现"用了但哪都没定义"的名字。

    这条挡的是拆文件时最常犯的错：
        方法搬到新文件了，但它用的东西没跟着 import。

    表现是窗口一开就 NameError，而 py_compile 完全看不出来。
    """
    bad = []
    for p in _all_desktop_py():
        try:
            miss = _undefined_names(p)
        except SyntaxError as e:
            bad.append(f"{os.path.relpath(p, ROOT)}: 语法错 {e}")
            continue
        if miss:
            rel = os.path.relpath(p, ROOT)
            detail = ", ".join(f"第{ln}行 {n}" for ln, n in miss[:8])
            bad.append(f"{rel}: {detail}")

    assert not bad, (
        "这些文件里有「用了但没定义也没 import」的名字 —— "
        "桌面端一开就会 NameError：\n  " + "\n  ".join(bad)
    )


def test_mixins_import_widgets_they_use():
    """mixin 里用到的自绘控件，必须在文件里 import。

    单独再查一遍，因为这是拆文件时踩得最狠的坑：
        services_mixin 用了 ServiceCard 但没 import，
        窗口画到服务页就崩。
    """
    mixdir = os.path.join(DESKTOP, "mixins")
    if not os.path.isdir(mixdir):
        return

    # widgets.py 里的类名
    widgets_py = os.path.join(DESKTOP, "widgets.py")
    if not os.path.exists(widgets_py):
        return
    tree = ast.parse(open(widgets_py, encoding="utf-8").read())
    widget_names = {
        n.name for n in tree.body if isinstance(n, ast.ClassDef)
    }

    bad = []
    for fn in sorted(os.listdir(mixdir)):
        if not fn.endswith(".py") or fn == "__init__.py":
            continue
        p = os.path.join(mixdir, fn)
        src = open(p, encoding="utf-8").read()
        subtree = ast.parse(src)

        defined = _collect_names(subtree)
        for name in widget_names:
            if name in src and name not in defined:
                bad.append(f"mixins/{fn} 用了 {name} 但没 import")

    assert not bad, "\n  ".join(["mixin 里漏了控件的 import："] + bad)
