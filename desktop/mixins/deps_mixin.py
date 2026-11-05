"""设置 · DepsPageMixin。

⚠️ 这是从 desktop/app.py 里搬出来的 —— 只搬方法，逻辑一个字没改。

    用 mixin 而不是"独立 widget"的原因：

        这些方法大量共享 self.tasks / self.api / self.filter /
        self._profiles 这些状态。
        改成独立 widget 要重新设计数据流，风险大得多；
        mixin 只是把方法换个文件放，self 还是 MainWindow。

    拆出来的好处：
        想改这一页 → 只开这个文件，不用在 3000 行里翻。
"""

from __future__ import annotations

# ── Qt ──
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from theme import (
    C_ACCENT, C_ACCENT2, C_ACCENT3, C_BAD, C_BG, C_DIM, C_DIM2,
    C_GRAD_A, C_GRAD_B, C_INPUT, C_LINE, C_LINE_HI, C_OK, C_RAISED,
    C_RUN, C_SURFACE, C_TEXT, C_TEXT2, C_WARN,
    STATUS_TEXT, _text_width, font, money, secs, status_color, toks,
)


class DepsPageMixin:

    def _build_deps_page(self) -> QWidget:
        """运行环境：缺什么一眼看到，点一下装上。

        布局为什么这样排
            上一版是"名字框固定宽度 + 说明框铺满"，
            结果名字框和版本号之间空一大块，右边也空着，很难看。

            现在一行三块：
                [●] 名字        说明（版本号或用途）        [按钮] [必需/可选]
            按钮靠右对齐，名字宽度按最长的那个定死，
            这样每一行都对齐，不会有的宽有的窄。
        """
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(12)

        # ── 标题（现在嵌在「系统」页里，降级成小节标题，别跟大标题抢）──
        head = QVBoxLayout()
        head.setSpacing(4)
        h = QLabel("运行环境")
        h.setFont(font(13, QFont.DemiBold))
        h.setStyleSheet(f"color:{C_TEXT};")
        sub = QLabel("跑起来需要什么，缺什么，这里都能看到。")
        sub.setFont(font(11))
        sub.setStyleSheet(f"color:{C_DIM};")
        head.addWidget(h)
        head.addWidget(sub)
        lay.addLayout(head)

        # ── 环境信息 ──
        self.lbl_env = QLabel("")
        self.lbl_env.setFont(font(10))
        self.lbl_env.setStyleSheet(f"color:{C_DIM2};")
        self.lbl_env.setWordWrap(True)
        lay.addWidget(self.lbl_env)

        lay.addSpacing(4)

        # ── 依赖列表 ──
        #
        # 整体一个卡片，行与行之间一条发丝线。
        # 不再每行单独加边框——那样看着像被切成好几块。
        card = QFrame()
        card.setObjectName("depcard")
        card.setStyleSheet(
            f"QFrame#depcard{{background:{C_SURFACE};"
            f"border:1px solid {C_LINE}; border-radius:10px;}}"
        )
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(14, 7, 14, 7)
        card_lay.setSpacing(0)

        self.deps_box = QVBoxLayout()
        self.deps_box.setSpacing(0)
        card_lay.addLayout(self.deps_box)
        lay.addWidget(card)

        lay.addStretch()

        # ── 底部：总进度 + 按钮 ──
        self.prog_deps = QProgressBar()
        self.prog_deps.setRange(0, 100)
        self.prog_deps.setValue(0)
        self.prog_deps.setTextVisible(False)
        self.prog_deps.setFixedHeight(6)
        self.prog_deps.setStyleSheet(
            f"QProgressBar{{background:{C_LINE}; border:none; border-radius:3px;}}"
            f"QProgressBar::chunk{{background:{C_ACCENT}; border-radius:3px;}}"
        )
        self.prog_deps.hide()
        lay.addWidget(self.prog_deps)

        self.lbl_prog = QLabel("")
        self.lbl_prog.setFont(font(10))
        self.lbl_prog.setStyleSheet(f"color:{C_DIM};")
        self.lbl_prog.setWordWrap(True)
        self.lbl_prog.hide()
        lay.addWidget(self.lbl_prog)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_install = QPushButton("装上缺的")
        self.btn_install.setCursor(Qt.PointingHandCursor)
        self.btn_install.setFixedHeight(36)
        self.btn_install.setMinimumWidth(120)
        self.btn_install.clicked.connect(self._install_deps)

        self.btn_recheck = QPushButton("重新检查")
        self.btn_recheck.setCursor(Qt.PointingHandCursor)
        self.btn_recheck.setFixedHeight(36)
        self.btn_recheck.setObjectName("ghost")
        self.btn_recheck.clicked.connect(self._refresh_deps)

        btn_row.addWidget(self.btn_install)
        btn_row.addWidget(self.btn_recheck)
        btn_row.addStretch()
        lay.addLayout(btn_row)

        self._dep_rows: dict[str, dict] = {}
        self._installing = False
        return page

    def _refresh_deps(self) -> None:
        """重新查一遍，重画列表。"""
        try:
            from tools import deps as _deps
        except ImportError:
            lbl = getattr(self, "lbl_env", None)
            if lbl:
                lbl.setText("读不到 deps 模块。")
            return

        # 清空
        while self.deps_box.count():
            item = self.deps_box.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._dep_rows.clear()

        info = _deps.env_info()
        lbl = getattr(self, "lbl_env", None)
        if lbl:
            lbl.setText(f"Python {info['python']}   ·   {info['platform']}")

        items = _deps.check_all()

        # 名字列按最长的定宽，保证对齐
        name_w = max((_text_width(it["label"]) for it in items), default=100)

        for idx, it in enumerate(items):
            # 行之间加一条发丝线（第一行不加）
            if idx > 0:
                line = QFrame()
                line.setFixedHeight(1)
                line.setStyleSheet(f"background:{C_LINE}; border:none;")
                self.deps_box.addWidget(line)

            row = self._make_dep_row(it, _deps, name_w)
            self.deps_box.addWidget(row)
            self._dep_rows[it["key"]] = it

        # 按钮（新系统页没有这个按钮，容错）
        btn = getattr(self, "btn_install", None)
        if not btn:
            return
        miss = _deps.missing(items)
        if self._installing:
            btn.setEnabled(False)
            btn.setText("正在装…")
        elif miss:
            btn.setEnabled(True)
            btn.setText(f"装上缺的（{len(miss)} 个）")
        else:
            btn.setEnabled(False)
            btn.setText("都齐了")

    def _make_dep_row(self, it: dict, _deps, name_w: int) -> QWidget:
        """造一行。跟设计稿 table td 对齐：padding 10px，发丝线隔行。

        排版：
            [●绿pill] Python  3.12.10              [灰pill 必需]
            [●琥珀pill] python-docx  未装          [小按钮 装它]
            [●灰pill] AstrBot  未找到              [小按钮 选择…]
        """
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 10, 0, 10)
        rl.setSpacing(10)

        # ── 状态点胶囊 ──
        dot = QLabel("●")
        dot.setFixedSize(20, 20)
        dot.setAlignment(Qt.AlignCenter)
        dot.setFont(font(9))
        if it["installed"]:
            dot.setStyleSheet(
                f"color:{C_OK}; background:rgba(95,198,126,.14);"
                f"border-radius:10px; border:none;"
            )
        else:
            dot.setStyleSheet(
                f"color:{C_ACCENT}; background:rgba(224,168,60,.14);"
                f"border-radius:10px; border:none;"
            )
        rl.addWidget(dot)

        # ── 名字 + 版本（同一行，名字常规、版本加粗）──
        name = QLabel(it["label"])
        name.setFont(font(12.5))
        name.setStyleSheet(f"color:{C_TEXT}; border:none;")
        rl.addWidget(name)

        ver_text = it.get("version") or ""
        if not ver_text:
            ver_text = it.get("note") or it.get("why") or "未装"
        ver = QLabel(ver_text)
        ver.setFont(font(12.5, QFont.DemiBold))
        ver.setStyleSheet(f"color:{C_TEXT2}; border:none;")
        rl.addWidget(ver)

        rl.addStretch(1)

        # ── 右边：按钮或灰胶囊标签 ──
        # ⚠️ 原来这里有个「选择…」按钮，只在 can_pick=True 时出现。
        #    全项目唯一会设 can_pick 的是 AstrBot 那条 ——
        #    它已经删了，所以这个分支成了死代码。
        #    留着的话：真点到会调 _pick_dep_path → save_custom_path（也删了）→ AttributeError。
        if not it["installed"] and it.get("can_install"):
            one = QPushButton("装它")
            one.setCursor(Qt.PointingHandCursor)
            one.setFixedHeight(26)
            one.setStyleSheet(
                f"QPushButton{{color:{C_TEXT}; background:#22262e;"
                f"border:1px solid #353b46; border-radius:7px;"
                f"padding:0 12px; font-size:11.5px;}}"
                f"QPushButton:hover{{background:#262b34;}}"
            )
            pip_name = it.get("pip") or it["label"]
            one.clicked.connect(
                lambda _=False, pk=pip_name: self._install_one(pk)
            )
            rl.addWidget(one)
        else:
            tag = QLabel("必需" if it.get("needed") else "可选")
            tag.setFont(font(10.5))
            tag.setAlignment(Qt.AlignCenter)
            tag.setStyleSheet(
                f"color:{C_DIM}; background:#262b33;"
                f"border-radius:999px; padding:2px 10px; border:none;"
            )
            rl.addWidget(tag)

        return row

    # ⚠️ _pick_dep_path 已删：它只给 AstrBot 那条「选择…」用，
    #    而 AstrBot 那条不在了，save_custom_path 也删了。
    #    留着是个必崩的死方法。（2026-11-05）

    def _install_one(self, pip_name: str) -> None:
        """装单个包。"""
        self._run_install([pip_name])

    def _install_deps(self) -> None:
        """一键装所有缺的。"""
        if self._installing:
            return
        try:
            from tools import deps as _deps
        except ImportError:
            return

        miss = _deps.missing(_deps.check_all())
        pkgs = [m["pip"] for m in miss if m.get("pip")]
        if not pkgs:
            return
        self._run_install(pkgs)

    def _run_install(self, pkgs: list[str]) -> None:
        """真正去装。"""
        try:
            from tools import deps as _deps
        except ImportError:
            return

        self._installing = True
        self.btn_install.setEnabled(False)
        self.btn_install.setText("正在装…")
        self.prog_deps.show()
        self.prog_deps.setRange(0, 100)
        self.prog_deps.setValue(0)
        self.lbl_prog.show()
        self.lbl_prog.setStyleSheet(f"color:{C_DIM};")
        self.lbl_prog.setText(f"开始装 {len(pkgs)} 个…")

        def on_progress(msg: str, pct: int) -> None:
            QTimer.singleShot(0, lambda: self._on_install_progress(msg, pct))

        def on_done(ok: bool, msg: str) -> None:
            QTimer.singleShot(0, lambda: self._on_install_done(ok, msg))

        _deps.install_async(pkgs, on_progress, on_done)

    def _on_install_progress(self, msg: str, pct: int) -> None:
        if pct < 0:
            self.prog_deps.setRange(0, 0)
        else:
            self.prog_deps.setRange(0, 100)
            self.prog_deps.setValue(pct)
        self.lbl_prog.setText(msg[:180])

    def _on_install_done(self, ok: bool, msg: str) -> None:
        self._installing = False
        self.prog_deps.setRange(0, 100)
        self.prog_deps.setValue(100 if ok else 0)

        if ok:
            self.lbl_prog.setText("装好了")
            self.lbl_prog.setStyleSheet(f"color:{C_OK};")
            QTimer.singleShot(800, self._refresh_deps)
        else:
            self.lbl_prog.setText(msg[:300])
            self.lbl_prog.setStyleSheet(f"color:{C_BAD};")
            self.btn_install.setEnabled(True)
            self.btn_install.setText("再试一次")

    # ── 服务页 ──
