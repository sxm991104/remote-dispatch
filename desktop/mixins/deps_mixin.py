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

        # ── 标题 ──
        head = QVBoxLayout()
        head.setSpacing(4)
        h = QLabel("环境")
        h.setFont(font(19, QFont.Bold))
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
            self.lbl_env.setText("读不到 deps 模块。检查一下 deps.py 在不在。")
            return

        # 清空
        while self.deps_box.count():
            item = self.deps_box.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._dep_rows.clear()

        info = _deps.env_info()
        self.lbl_env.setText(f"Python {info['python']}   ·   {info['platform']}")

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

        # 按钮
        miss = _deps.missing(items)
        if self._installing:
            self.btn_install.setEnabled(False)
            self.btn_install.setText("正在装…")
        elif miss:
            self.btn_install.setEnabled(True)
            self.btn_install.setText(f"装上缺的（{len(miss)} 个）")
        else:
            # ⚠️ 都齐了也不能把按钮藏起来——
            #    用户看不到这个按钮，就不知道有这个功能。
            self.btn_install.setEnabled(False)
            self.btn_install.setText("都齐了")

    def _make_dep_row(self, it: dict, _deps, name_w: int) -> QWidget:
        """造一行。

        排版目标：
            ●  Python          3.12.10              必需
            ●  python-docx     1.2.0                可选
            ●  AstrBot         D:\\qqaibot\\astrbot  [选择…]  可选

        三列对齐，中间那列吃掉多余宽度。
        不逐个加边框——那样看着像被切开的几块。
        """
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(2, 7, 2, 7)
        rl.setSpacing(0)

        # ── 状态点 ──
        dot = QLabel("●")
        dot.setFont(font(9))
        dot.setFixedWidth(16)
        dot.setAlignment(Qt.AlignCenter)
        if it["installed"]:
            dot.setStyleSheet(f"color:{C_OK}; border:none;")
        elif it.get("needed"):
            dot.setStyleSheet(f"color:{C_BAD}; border:none;")
        else:
            dot.setStyleSheet(f"color:{C_WARN}; border:none;")
        rl.addWidget(dot)

        # ── 名字（固定宽度，保证对齐）──
        name = QLabel(it["label"])
        name.setFont(font(11, QFont.Bold))
        name.setStyleSheet(f"color:{C_TEXT}; border:none;")
        name.setFixedWidth(name_w)
        rl.addWidget(name)

        rl.addSpacing(12)

        # ── 说明（吃掉剩余空间）──
        desc_text = it.get("version") or ""
        if not desc_text:
            desc_text = it.get("note") or it.get("why") or ""
        desc = QLabel(desc_text)
        desc.setFont(font(10))
        desc.setStyleSheet(f"color:{C_DIM}; border:none;")
        desc.setWordWrap(False)
        # 关键：设成 Expanding，让它真正吃掉中间的空间，
        # 而不是每列各占一块、中间留着空洞。
        desc.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        rl.addWidget(desc, 1)

        # ── 右边：按钮 + 标签 ──
        right = QHBoxLayout()
        right.setSpacing(6)

        if it.get("can_pick"):
            pick = QPushButton("选择…")
            pick.setCursor(Qt.PointingHandCursor)
            pick.setFixedHeight(24)
            pick.setStyleSheet(
                f"QPushButton{{color:{C_DIM}; background:transparent;"
                f"border:1px solid {C_LINE}; border-radius:6px;"
                f"padding:0 10px; font-size:11px;}}"
                f"QPushButton:hover{{background:{C_LINE};}}"
            )
            key = it["key"]
            pick.clicked.connect(lambda _=False, k=key: self._pick_dep_path(k))
            right.addWidget(pick)

        if not it["installed"] and it.get("can_install"):
            one = QPushButton("装它")
            one.setCursor(Qt.PointingHandCursor)
            one.setFixedHeight(24)
            one.setStyleSheet(
                f"QPushButton{{color:{C_ACCENT}; background:transparent;"
                f"border:1px solid {C_ACCENT}; border-radius:6px;"
                f"padding:0 10px; font-size:11px;}}"
                f"QPushButton:hover{{background:{C_ACCENT}; color:white;}}"
            )
            pip_name = it.get("pip") or it["label"]
            one.clicked.connect(
                lambda _=False, pk=pip_name: self._install_one(pk)
            )
            right.addWidget(one)

        tag = QLabel("必需" if it.get("needed") else "可选")
        tag.setFont(font(9))
        tag.setAlignment(Qt.AlignCenter)
        tag.setFixedWidth(34)
        if it.get("needed"):
            tag.setStyleSheet(
                f"color:{C_DIM2}; border:1px solid {C_LINE};"
                f"border-radius:4px; padding:1px 0;"
            )
        else:
            tag.setStyleSheet(
                f"color:{C_DIM2}; border:1px solid {C_LINE};"
                f"border-radius:4px; padding:1px 0;"
            )
        right.addWidget(tag)

        rl.addLayout(right)
        return row

    def _pick_dep_path(self, key: str) -> None:
        """让用户手动指一个目录。"""
        try:
            from tools import deps as _deps
        except ImportError:
            return

        d = QFileDialog.getExistingDirectory(
            self, "选择 AstrBot 的目录", ""
        )
        if not d:
            return

        _deps.save_custom_path(key, d)
        self._refresh_deps()

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
