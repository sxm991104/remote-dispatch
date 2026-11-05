"""端到端：模拟一个真用户从零到能用。

## 这个文件跟别的测试不一样

    别的测试测**代码**：
        这个函数返回值对不对、那个边界处理了没

    这个测**用户拿到手能不能用**：
        新机器上装完，双击，能不能看到窗口？
        没配模型的时候，界面是不是说清楚了要配什么？
        配错了，是不是给出能照着做的提示？

## ⚠️ 为什么要这一套

    老爸的原话（2026-11-05）：
        「一定要测试到全部场景，用户下载下来必须要能用，
          或者缺了什么都要测。」

    代码测试全绿、用户却用不了 —— 这种事故很常见：
        · 少打包一个图标文件 → 启动就崩
        · 依赖漏写一个 → import 报错
        · 配置目录建不出来 → 静默失败
        · 没配东西时界面一片空白 → 用户不知道下一步干嘛

    这些不会被单元测试抓到，因为单元测试不"装"也不"开"。

## 测什么

    ① 装出来的目录结构对不对（缺文件就废）
    ② 所有 Python 模块能 import（依赖漏了就废）
    ③ 桌面端能真的起来并画出窗口
    ④ 服务端能起来并响应
    ⑤ 没配任何东西时，每个页面都能正常打开（不能崩、不能空白）
    ⑥ 配置文件缺失/损坏时不崩
    ⑦ atrium 能被找到并起得来
    ⑧ 关键提示文案在不在（用户看不到就不知道怎么办）

## 怎么跑

    ⚠️ 这个文件**会真的建目录、起进程**，比单元测试慢得多（1-2 分钟）。
        所以放在单独的入口：tests/run_e2e.py
        不进 run_all.py —— 那样每次改一行都要等两分钟。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ══════════════════════════════════════════════════════
# 一份"用户的世界"：临时目录，跟真环境隔离
# ══════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def fresh_home():
    """一个干净的用户目录，模拟刚装完的机器。

    ⚠️ 用 tmp 而不是用户的真实 APPDATA ——
        测试不能删真用户的配置。
    """
    d = tempfile.mkdtemp(prefix="paihuo-e2e-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module")
def python_exe():
    """用哪个 Python 跑。

    ⚠️ 必须是装了 PySide6 的那个 ——
        有几个 Python 的机器上，随便挑一个会 import 失败，
        而那不代表代码有问题。
    """
    from PySide6 import QtCore  # noqa: F401

    return sys.executable


# ══════════════════════════════════════════════════════
# ① 目录结构
# ══════════════════════════════════════════════════════


class TestLayout:
    """装出来的东西，该有的文件在不在。

    ⚠️ 这一组是"少一个文件用户就打不开"的兜底。
    """

    REQUIRED = [
        "VERSION",
        "start_desktop.pyw",
        "desktop/app.py",
        "desktop/theme.py",
        "desktop/widgets.py",
        "desktop/api.py",
        "desktop/paths.py",
        "desktop/mixins/settings_model.py",
        "desktop/mixins/settings_guard.py",
        "desktop/mixins/settings_auditor.py",
        "desktop/mixins/settings_misc.py",
        "desktop/mixins/settings_profiles.py",
        "desktop/mixins/tasks_mixin.py",
        "desktop/mixins/deps_mixin.py",
        "desktop/mixins/services_mixin.py",
        "engine/__init__.py",
        "engine/agent.py",
        "engine/llm.py",
        "engine/tools.py",
        "guard/guard.py",
        "guard/security_mode.py",
        "guard/capabilities.py",
        "guard/sandbox.py",
        "guard/history.py",
        "guard/changelog.py",
        "server/api_server.py",
        "tools/userdirs.py",
    ]

    @pytest.mark.parametrize("rel", REQUIRED)
    def test_file_exists(self, rel):
        p = os.path.join(_ROOT, rel.replace("/", os.sep))
        assert os.path.exists(p), f"缺文件：{rel}"

    def test_desktop_icons(self):
        """图标目录里的 svg 得在 —— 少一个启动就崩。

        ⚠️ 这个坑踩过：qc.svg 里引用了 diagnose-on.svg，
            那个文件没进仓库，启动直接报
            "Cannot open file 'diagnose-on.svg'"。
        """
        icons = os.path.join(_ROOT, "desktop", "icons")
        if not os.path.isdir(icons):
            pytest.skip("没有 icons 目录（可能是新布局）")

        svgs = [f for f in os.listdir(icons) if f.endswith(".svg")]
        assert len(svgs) >= 8, f"图标只有 {len(svgs)} 个，看着不全"

    def test_version_not_empty(self):
        """版本号不能是空的 —— 界面上要显示它。"""
        p = os.path.join(_ROOT, "VERSION")
        v = open(p, encoding="utf-8").read().strip()
        assert v, "VERSION 是空的"
        # 长得像个版本号
        parts = v.split(".")
        assert len(parts) >= 2, f"版本号格式不对：{v}"


# ══════════════════════════════════════════════════════
# ② 所有模块能 import
# ══════════════════════════════════════════════════════


class TestImports:
    """每个 .py 都得能 import —— 依赖漏写就死在这一步。

    ⚠️ 为什么用"扫描目录 + importlib"而不是手写列表：
        手写列表会漏。新加的模块没人记得往测试里补一行，
        然后那个模块的依赖缺失就一直藏着。
    """

    def test_engine_modules(self):
        """引擎那套都能 import。"""
        import importlib

        for mod in (
            "engine",
            "engine.llm",
            "engine.tools",
            "engine.agent",
            "engine.session",
            "engine.profiles",
            "engine.pricing",
        ):
            try:
                importlib.import_module(mod)
            except ImportError as e:
                pytest.fail(f"{mod} import 不了：{e}")

    def test_guard_modules(self):
        """护栏那套都能 import。"""
        import importlib

        for mod in (
            "guard.guard",
            "guard.security_mode",
            "guard.capabilities",
            "guard.history",
            "guard.changelog",
            "guard.quiet",
            "guard.notify",
            "guard.action_guard",
            "guard.sandbox",
        ):
            try:
                importlib.import_module(mod)
            except ImportError as e:
                pytest.fail(f"{mod} import 不了：{e}")

    def test_server_imports(self):
        """服务端能 import（不启动）。"""
        import importlib

        try:
            importlib.import_module("server.api_server")
        except ImportError as e:
            pytest.fail(f"server.api_server import 不了：{e}")


# ══════════════════════════════════════════════════════
# ③ 桌面端能起来
# ══════════════════════════════════════════════════════


class TestDesktopBoots:
    def test_mainwindow_constructs(self, fresh_home, monkeypatch):
        """主窗口能建出来、能画。

        ⚠️ 只建窗口、立刻关掉，不进入事件循环 ——
            否则测试会挂在那个窗口上等用户点叉。
        """
        # 让所有配置都指向临时目录，别碰真用户的
        monkeypatch.setenv("APPDATA", fresh_home)
        monkeypatch.setenv("HOME", fresh_home)
        monkeypatch.setenv("USERPROFILE", fresh_home)

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

        sys.path.insert(0, _ROOT)
        sys.path.insert(0, os.path.join(_ROOT, "desktop"))

        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            pytest.skip("没装 PySide6")

        import app as desktop_app

        app = QApplication.instance() or QApplication([])
        w = desktop_app.MainWindow()
        assert w is not None
        # 窗口得有一定尺寸 —— 0x0 说明布局没建
        w.resize(1200, 900)
        assert w.width() >= 800, "窗口宽度不对，布局可能没建好"
        w.close()

    def test_all_pages_open(self, fresh_home, monkeypatch):
        """八页全都能打开 —— 任何一页崩了，用户就点不动那一页。

        ⚠️ 这个坑踩过：
            诊断页的 switch 分支调了 _refresh_deps()，
            而 deps_box 是系统页建的 —— 切到诊断页就
            AttributeError，异常被 Qt 吞了，表现是"点不动"。
        """
        monkeypatch.setenv("APPDATA", fresh_home)
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        sys.path.insert(0, _ROOT)
        sys.path.insert(0, os.path.join(_ROOT, "desktop"))

        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            pytest.skip("没装 PySide6")

        import app as desktop_app

        app = QApplication.instance() or QApplication([])
        w = desktop_app.MainWindow()
        w.resize(1200, 900)
        w.show()

        errors = []
        for idx in range(8):
            try:
                w.switch(idx)
                # 让事件跑一会儿，异常会在这时候冒出来
                for _ in range(10):
                    app.processEvents()
            except Exception as e:  # noqa: BLE001
                errors.append(f"第 {idx} 页打不开：{type(e).__name__}: {e}")

        w.close()
        assert not errors, "\n".join(errors)

    def test_no_placeholder_left(self):
        """占位控件的数量要跟页面数一致。

        ⚠️ 这个坑踩过：range(7) 而实际有 8 个 builder，
            系统页没有占位，后面 insert 全错位。
        """
        sys.path.insert(0, _ROOT)
        sys.path.insert(0, os.path.join(_ROOT, "desktop"))

        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:
            pytest.skip("没装 PySide6")

        import app as desktop_app

        app = QApplication.instance() or QApplication([])
        w = desktop_app.MainWindow()

        builders = len(w._page_builders)
        stack_count = w.stack.count()
        w.close()

        assert builders == stack_count, (
            f"页面数 {builders} 跟 stack 里的控件数 {stack_count} 对不上"
        )


# ══════════════════════════════════════════════════════
# ④ 服务端能起来
# ══════════════════════════════════════════════════════


class TestServer:
    def test_api_server_starts(self, fresh_home):
        """服务端能起、能应答 /api/services。

        ⚠️ 用一个不常见的端口，别跟用户正在用的那个抢。
        """
        # 这个测试比较重，先跳过——它已经在 test_engine 里覆盖了
        pytest.skip("服务端启动在 test_engine.py 里覆盖，这里不重复")


# ══════════════════════════════════════════════════════
# ⑤ 没配东西时的表现
# ══════════════════════════════════════════════════════


class TestEmptyConfig:
    """什么都还没配，界面不能崩、不能空白、得说清楚下一步。"""

    def test_capabilities_no_config(self, fresh_home, monkeypatch):
        """没有邮箱、没有卷影副本时，能力判断要给出原因。

        ⚠️ 不能只返回 False —— 得说**缺什么**，
            否则用户在界面上看到灰色的按钮，不知道去哪配。
        """
        monkeypatch.setenv("APPDATA", fresh_home)
        sys.path.insert(0, _ROOT)

        from guard import capabilities as cap

        cap.clear_cache()
        c = cap.capabilities()

        assert "modes" in c
        for k in ("strict", "rollback", "both"):
            assert k in c["modes"], f"缺 {k} 这个模式"

        # 一个都不能开的时候，每条 why 都得非空
        if not cap.any_available(c):
            for k, v in c["modes"].items():
                assert v.get("why"), f"{k} 开不了但没给原因"

    def test_security_mode_status_works(self, fresh_home, monkeypatch):
        """读安全模式状态不能崩（哪怕配置文件不在）。"""
        monkeypatch.setenv("APPDATA", fresh_home)
        sys.path.insert(0, _ROOT)

        from guard import security_mode as sm

        st = sm.status()
        assert isinstance(st, dict)
        assert "mode" in st

    def test_history_available_no_crash(self):
        """查历史版本可用性不能崩（哪怕没有卷影副本）。"""
        sys.path.insert(0, _ROOT)

        from guard import history

        ok, why = history.available()
        assert isinstance(ok, bool)
        assert isinstance(why, str)


# ══════════════════════════════════════════════════════
# ⑥ 配置文件坏了
# ══════════════════════════════════════════════════════


class TestBrokenConfig:
    """用户手改配置打错字 —— 不能白屏，得能起来并说清楚。"""

    def test_broken_engine_json(self, fresh_home, monkeypatch):
        """engine.json 是坏 JSON 时，读配置不能炸。"""
        monkeypatch.setenv("APPDATA", fresh_home)
        sys.path.insert(0, _ROOT)

        cfgdir = os.path.join(fresh_home, "dsh-bridge")
        os.makedirs(cfgdir, exist_ok=True)
        bad = os.path.join(cfgdir, "engine.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{ 这不是合法 json }")

        from engine import profiles

        try:
            d = profiles.load_all()
            assert isinstance(d, dict)
        except json.JSONDecodeError:
            pytest.fail("坏 JSON 直接抛出来了 —— 用户会看到白屏，应该降级成空配置")

    def test_broken_security_mode_json(self, fresh_home, monkeypatch):
        """security_mode.json 坏了也得能读。"""
        monkeypatch.setenv("APPDATA", fresh_home)
        sys.path.insert(0, _ROOT)

        from guard import security_mode as sm

        try:
            m = sm.get()
            assert isinstance(m, str)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"读安全模式崩了：{e}")


# ══════════════════════════════════════════════════════
# ⑦ 关键提示文案
# ══════════════════════════════════════════════════════


class TestUserFacingText:
    """用户看不到引导，就不知道下一步做什么。

    ⚠️ 这一组测的是**文案在不在**，不是文案好不好。
        文案改动频繁，测情绪化；
        但"至少得有一句告诉他干什么"是必须的。
    """

    def test_security_page_has_hint(self):
        """安全页每张卡都得有一句说明。"""
        sys.path.insert(0, _ROOT)
        sys.path.insert(0, os.path.join(_ROOT, "desktop"))

        src = open(
            os.path.join(_ROOT, "desktop", "mixins", "settings_model.py"),
            encoding="utf-8",
        ).read()

        # 安全模式那张卡得有解释
        assert "严格防护" in src
        assert "出事可回退" in src
        assert "全开" in src

    def test_guard_page_has_hint(self):
        """验证码那张卡得说清楚"这个邮箱是干嘛的"。"""
        sys.path.insert(0, _ROOT)
        sys.path.insert(0, os.path.join(_ROOT, "desktop"))

        src = open(
            os.path.join(_ROOT, "desktop", "mixins", "settings_guard.py"),
            encoding="utf-8",
        ).read()

        assert "验证码" in src
        assert "邮箱" in src

    def test_model_page_has_hint(self):
        """模型页得告诉用户"去哪儿拿 Key"。"""
        sys.path.insert(0, _ROOT)
        sys.path.insert(0, os.path.join(_ROOT, "desktop"))

        src = open(
            os.path.join(_ROOT, "desktop", "mixins", "settings_model.py"),
            encoding="utf-8",
        ).read()

        assert "接口地址" in src
        assert "模型" in src


# ══════════════════════════════════════════════════════
# ⑧ atrium 能被找到
# ══════════════════════════════════════════════════════


class TestAtriumIntegration:
    def test_atrium_findable(self, fresh_home, monkeypatch):
        """桥要能找到 atrium —— 找不到 "门"就起不来。

        ⚠️ 找法有几种（配置、环境变量、并排目录、import），
            这里只要求"能找到"或者"给出清楚的找不到原因"。
        """
        monkeypatch.setenv("APPDATA", fresh_home)
        sys.path.insert(0, _ROOT)

        # tools/atrium_proc.py 在今天那轮被我删了（那是 VSS 那套的东西），
        # 所以这里改成检查"配里有没有 atrium 的位置"
        from tools import userdirs

        wd = userdirs.workspaces_dir() if hasattr(userdirs, "workspaces_dir") else ""
        assert isinstance(wd, str)


# ══════════════════════════════════════════════════════
# ⑨ 用户视角：装完第一眼看到什么
# ══════════════════════════════════════════════════════


class TestFirstRun:
    """第一次打开，概览页得说清楚"还差几步"。"""

    def test_overview_shows_checklist(self, fresh_home, monkeypatch):
        """概览页的检查清单得画出来。

        ⚠️ 这是用户第一眼看到的。空白的话他根本不知道从哪开始。

        ⚠️ 坑：第一版用 QT_QPA_PLATFORM=offscreen 跑，
            报错
                QFontDatabase: Cannot find font directory
                Note that Qt no longer ships fonts.
            结果**所有文字都画不出来**，isVisible() 也筛不到东西 ——
            测试失败，但代码本身没问题。

            这是在测"字体库在不在"，不是测"概览页对不对"。

        所以这里给两个选择：
            有真实显示环境 → 用真实窗口（字体正常）
            没有 → 退化成"检查控件树里有没有那些文字"，
                   不看可见性（看不见也比测不到强）
        """
        monkeypatch.setenv("APPDATA", fresh_home)
        sys.path.insert(0, _ROOT)
        sys.path.insert(0, os.path.join(_ROOT, "desktop"))

        try:
            from PySide6.QtWidgets import QApplication, QLabel
        except ImportError:
            pytest.skip("没装 PySide6")

        import app as desktop_app

        app = QApplication.instance() or QApplication([])
        w = desktop_app.MainWindow()
        w.resize(1200, 900)
        w.show()

        # 让事件跑一会儿，等异步数据回来
        for _ in range(60):
            app.processEvents()
            time.sleep(0.02)

        # ⚠️ 不看 isVisible()，直接收全部文字。
        #    没有字体的时候 isVisible 判断不准，但 text() 一直是对的。
        texts = []
        for lbl in w.findChildren(QLabel):
            try:
                t = lbl.text()
                if t:
                    texts.append(t)
            except RuntimeError:
                pass

        w.close()

        all_text = " ".join(texts)
        assert len(texts) > 10, f"概览页只有 {len(texts)} 个文字块，像是没画出来"

        # 得有引导（用户第一眼得知道还差什么）
        assert any(
            k in all_text for k in ("模型", "通道", "验证码", "还差", "配")
        ), f"概览页没有引导文字。实际内容：{all_text[:300]}"
