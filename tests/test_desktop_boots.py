"""桌面端能不能真的画出来。

## 为什么需要这个

    2026-11-02 晚上拆 desktop/app.py（4311 行 → 18 个文件）的过程中，
    连着犯了三种"静态能过、一用就炸"的错：

        一、mixin 里用了 QWidget / ServiceCard，但文件没 import
            → py_compile 过，静态检查过，**窗口一开就 NameError**

        二、tools/deps.py 里 _userdirs 的 import 忘了加
            → 窗口能开，**一点「环境」页就崩**

        三、LoginQRDialog / DetailDialog 没 import
            → 窗口能开，**一点「启动」就崩**

    前一种已经加了 tests/test_static_names.py 拦。
    但后两种是**运行某个函数时才炸**，静态扫不出来 ——
    只有真的把窗口建出来、把每一页切一遍，才知道。

## 这个测试做什么

    起一个真的 QApplication（不需要显示器，Qt 有 offscreen 平台），

        一、把 MainWindow 建出来
        二、切到四个页面
        三、只切一次 —— 不重复构造同一张卡

    ⚠️ 关于"每张卡单独构造一次"：
        原来我也测了，但那会重复构造（卡片在 __init__ 里已经建过），
        Qt 会把前一个 C++ 对象回收掉，后面的调用就报
        "Internal C++ object already deleted"。
        那是**测试写法的问题**，不是代码的问题。
        所以现在只走真实路径：MainWindow() 建一次，然后切页。

## 它测不到的

    · 点按钮之后的网络请求（要真服务）
    · 弹窗里的具体内容
    · 绘制效果

    这些交给别的测试和人工。这条只保证一件事：
    **窗口能建出来，每一页都能进。**
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Qt 在无显示器环境要这个，不然起不来
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="桌面端要 PySide6")


@pytest.fixture(scope="module")
def main_window():
    """建一个 MainWindow，整个模块共用。

    ⚠️ 不能在每个测试里各建一个 —— 会重复构造卡片，
        Qt 回收前一个 C++ 对象，后面的调用就炸。
    """
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "desktop"))

    spec = importlib.util.spec_from_file_location(
        "appmod_for_test", os.path.join(ROOT, "desktop", "app.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    w = mod.MainWindow()
    yield w

    try:
        w.close()
        w.deleteLater()
    except Exception:  # noqa: BLE001
        pass


def test_main_window_constructs(main_window):
    """MainWindow 能建出来。

    这一条挂了，说明桌面端根本打不开 ——
    而且原因通常是"某个方法里用了没 import 的东西"，
    静态检查拦不住，只有真建一次才知道。
    """
    assert main_window is not None
    assert main_window.windowTitle() == "dsh-bridge"


def test_all_pages_switch(main_window):
    """四个页面都能切过去。

    ⚠️ 这条抓到过真 bug：
        tools/deps.py 里 _userdirs 的 import 忘了加，
        切到「环境」页就 NameError。

        它从 1.3.3 一路潜伏到现在 —— 因为没人切过那一页，
        而 274 条回归测试里也没有一条去建界面。
    """
    for i, name in enumerate(("任务", "服务", "环境", "设置")):
        main_window.switch(i)   # 抛异常就是失败
        assert main_window.stack.currentIndex() == i, f"切到「{name}」页失败"


def test_settings_cards_exist(main_window):
    """设置页那几张卡的关键控件都在。

    切到设置页之后，各 _card_* 已经在 __init__ 里建过了，
    这里只确认它们留下的属性存在 —— 不再重复构造。
    """
    main_window.switch(3)

    for attr in (
        "switch_auditor", "lbl_auditor_state",
        "ed_auditor_url", "ed_auditor_key",
        "cmb_auditor_model", "lbl_auditor_msg",
        "profile_box", "btn_guard",
    ):
        assert hasattr(main_window, attr), f"设置页缺控件：{attr}"


def test_refresh_runs(main_window):
    """refresh() 不炸。

    它每 3 秒调一次，是界面活着的基础。
    """
    main_window.refresh()


def test_detail_dialog_constructs(main_window):
    """任务详情弹窗能建出来。

    ⚠️ 这条抓到过真 bug：
        DetailDialog 原来定义在 app.py 里，
        而 tasks_mixin 要用它 —— 会循环导入。
        搬到 dialogs.py 之后，这边再确认它真的建得出来。
    """
    from dialogs import DetailDialog

    d = DetailDialog(
        {"name": "测试任务", "status": "ok", "task": "做个测试"},
        main_window.api,
        main_window,
    )
    assert d is not None
    d.deleteLater()
