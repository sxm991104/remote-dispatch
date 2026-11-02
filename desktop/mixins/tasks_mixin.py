"""设置 · TasksPageMixin。

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
from PySide6.QtCore import Qt
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

# ── 自绘控件 ──
# 这些是 desktop/widgets.py 里的自绘控件 ——
# 拆文件时漏了它们，窗口一开就 NameError。
from widgets import (
    GlowDot,
    TaskRow,
)

from dialogs import DetailDialog
from theme import (
    C_ACCENT, C_ACCENT2, C_ACCENT3, C_BAD, C_BG, C_DIM, C_DIM2,
    C_GRAD_A, C_GRAD_B, C_INPUT, C_LINE, C_LINE_HI, C_OK, C_RAISED,
    C_RUN, C_SURFACE, C_TEXT, C_TEXT2, C_WARN,
    STATUS_TEXT, _text_width, font, money, secs, status_color, toks,
)


class TasksPageMixin:

    def _build_tasks_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(16)

        head = QVBoxLayout()
        head.setSpacing(4)
        h = QLabel("任务")
        h.setFont(font(19, QFont.Bold))
        h.setStyleSheet(f"color:{C_TEXT};")
        sub = QLabel("派出去让电脑干的活，都在这儿。")
        sub.setFont(font(12))
        sub.setStyleSheet(f"color:{C_DIM};")
        head.addWidget(h)
        head.addWidget(sub)
        lay.addLayout(head)

        # ⚠️ 派活输入框去掉了。
        #
        #    派活走飞书 / QQ 那边——用户直接跟丫头说。
        #    桌面上再放一个派活框，等于两条路都能派活，
        #    出了问题不知道是谁派的。

        # ⚠️ 筛选按钮挪到每个 tab 里面了。
        #
        #    放在这里的话，看起来像"筛所有模型"——
        #    但它实际是"筛当前这个模型里的任务"。
        #    层级放错，用户会以为切 tab 之后筛选还在管全局。
        #
        #    现在：先在 tab 里选模型，再在下面筛状态。

        # ⚠️ 这里不能加 addStretch()。
        #
        #    setWidgetResizable(True) 的意思是"让内容撑满可视区域"。
        #    而内容里如果有个弹簧，Qt 会陷入这样的循环：
        #        内容高度取决于可用空间 → 可用空间取决于滚动条
        #        → 滚动条取决于内容高度 → 回到起点
        #    算不完就一直在算，屏幕上表现为**整个区域持续闪烁**。
        #
        #    实测：光看代码看不出问题，因为它不是"多画了一次"，
        #    而是布局引擎在反复迭代。
        # ⚠️ 这里从"一个大滚动列表"改成了"每个模型一个 tab"。
        #
        #    原来：所有任务塞进一个 QScrollArea，平铺。
        #    现在：模型做 tab，每个 tab 里一个滚动区。
        #
        #    这样一屏只看一个模型的任务，不挤；
        #    要换模型点 tab，不用往下翻。

        self.model_tabs = QTabWidget()
        self.model_tabs.setObjectName("modeltabs")
        self.model_tabs.setStyleSheet(
            "QTabWidget::pane {"
            f" border:none; background:transparent;"
            "}"
            "QTabBar::tab {"
            f" background:transparent; color:{C_DIM};"
            " padding:7px 16px; margin-right:4px;"
            " border-bottom:2px solid transparent;"
            "}"
            "QTabBar::tab:hover {"
            f" color:{C_TEXT2};"
            "}"
            "QTabBar::tab:selected {"
            f" color:{C_TEXT}; border-bottom:2px solid {C_ACCENT};"
            "}"
        )

        # 每个 tab 的内容是一个滚动区，装这个模型名下的任务行
        self.task_tab_host = QWidget()
        self.task_tab_host_lay = QVBoxLayout(self.task_tab_host)
        self.task_tab_host_lay.setContentsMargins(0, 0, 0, 0)
        self.task_tab_host_lay.setSpacing(8)

        lay.addWidget(self.model_tabs, 1)


        return page

    def _set_filter(self, key: str) -> None:
        self.filter = key
        for k, b in self.chip_btns:
            b.setChecked(k == key)
        self._render_tasks()

    # ── 环境页 ──
    def _render_tasks(self) -> None:
        """每个模型一个 tab，tab 下面列它名下的任务。

        ⚠️ 不重建已经存在的 tab。
            列表每 3 秒刷一次，重建会让屏幕闪、主线程被占。
            所以先算指纹：哪些模型、每个模型下哪些任务。
            指纹没变 → 只更新每行文字。
            指纹变了 → 才重建 tab。
        """
        tabs = getattr(self, "model_tabs", None)
        if tabs is None:
            return

        profs = getattr(self, "_profiles", None) or []
        tasks = getattr(self, "tasks", None) or []

        # 筛选
        shown = tasks
        if self.filter == "running":
            shown = [t for t in tasks if t.get("status") == "running"]
        elif self.filter == "ok":
            shown = [t for t in tasks if t.get("status") == "ok"]
        elif self.filter == "bad":
            shown = [t for t in tasks if t.get("status") in ("error", "timeout")]

        groups: dict = {}
        for t in shown:
            groups.setdefault(t.get("profile") or "", []).append(t)

        order = {"running": 0, "pending": 1, "queued": 1}
        for k in groups:
            groups[k] = sorted(
                groups[k], key=lambda t: order.get(t.get("status"), 2)
            )

        # 没有配置时给一句提示
        if not profs:
            if getattr(self, "_render_sig", None) == ("noprof",):
                return
            self._clear_tabs()
            lb = QLabel(
                "还没有配置模型。去「设置」里加一套，任务才知道交给谁跑。"
            )
            lb.setFont(font(11))
            lb.setWordWrap(True)
            lb.setStyleSheet(f"color:{C_DIM2}; padding:16px 0;")
            self.task_tab_host.layout().addWidget(lb)
            self._render_sig = ("noprof",)
            return

        # ── 指纹 ──
        sig = tuple(
            (p.get("id") or "",
             tuple(t.get("name") for t in groups.get(p.get("id") or "", [])[:self._list_limit]))
            for p in profs
        )

        if getattr(self, "_render_sig", None) == sig:
            # 结构没变：只更新每行文字和状态
            for p in profs:
                pid = p.get("id") or ""
                block = getattr(self, "_task_blocks", {}).get(pid)
                if not block:
                    continue
                items = groups.get(pid, [])[:self._list_limit]
                for t, row in zip(items, block["rows"]):
                    row.update_data(t)
                block["stat"].setText(self._model_stat_text(items, p))
                block["dot"].setColor(C_OK if items else C_DIM2)
            return

        # ── 结构变了：重建 ──
        self._clear_tabs()
        self._task_blocks = {}
        # 筛选按钮跟着 tab 一起重建，旧的要清掉，
        # 不然 _set_filter 会去点已经删掉的按钮。
        self.chip_btns = []
        lay = self.task_tab_host.layout()

        for p in profs:
            pid = p.get("id") or ""
            items = groups.get(pid, [])[:self._list_limit]

            page = QWidget()
            pl = QVBoxLayout(page)
            pl.setContentsMargins(4, 10, 4, 4)
            pl.setSpacing(10)

            # ── 筛选条（只筛这个模型的任务） ──
            chips = QHBoxLayout()
            chips.setSpacing(6)
            for ckey, ctext in (("all", "全部"), ("running", "运行中"),
                                ("ok", "已完成"), ("bad", "失败")):
                c = QPushButton(ctext)
                c.setObjectName("chip")
                c.setCheckable(True)
                c.setChecked(ckey == self.filter)
                c.setCursor(Qt.PointingHandCursor)
                c.clicked.connect(
                    lambda _=False, k=ckey: self._set_filter(k)
                )
                chips.addWidget(c)
                self.chip_btns.append((ckey, c))
            chips.addStretch()
            pl.addLayout(chips)

            # ── 状态行 ──
            head = QHBoxLayout()
            head.setSpacing(8)

            dot = GlowDot(C_OK if items else C_DIM2, 7)
            head.addWidget(dot, 0, Qt.AlignVCenter)

            stat = QLabel(self._model_stat_text(items, p))
            stat.setFont(font(11))
            stat.setStyleSheet(f"color:{C_DIM};")
            head.addWidget(stat)
            head.addStretch()
            pl.addLayout(head)

            # ── 任务行 ──
            rows = []
            if items:
                for t in items:
                    row = TaskRow(t)
                    row.clicked.connect(self._open_task)
                    pl.addWidget(row)
                    rows.append(row)

            pl.addStretch()

            # ⚠️ 必须套一层 QScrollArea，不能把 page 直接塞进 tab。
            #
            #    原来直接 addTab(page) —— 任务一多（20 个以上），
            #    普通 QWidget 没有滚动条，布局管理器只能**强行压缩**：
            #    TaskRow 明明 setFixedHeight(60)，被压到 20 像素，
            #    文字全挤在一起看不清。
            #
            #    滚动区给了无限的纵向空间，行就能保持自己的高度。
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setStyleSheet("QScrollArea { border:none; background:transparent; }")
            scroll.setWidget(page)

            # 每套模型一个 tab。名字用配置里的 name。
            idx = self.model_tabs.addTab(scroll, p.get("name") or "未命名")
            self._task_blocks[pid] = {
                "rows": rows, "stat": stat, "dot": dot, "index": idx,
            }

        self._render_sig = sig

    def _clear_tabs(self) -> None:
        """清空所有 tab。"""
        tabs = getattr(self, "model_tabs", None)
        if tabs is None:
            return
        while tabs.count():
            w = tabs.widget(0)
            tabs.removeTab(0)
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _model_stat_text(self, items: list, prof: dict) -> str:
        """这个模型名下的状态：几个在跑、几个排队。

        ⚠️ 原来定义了两次（内容几乎一样），第二个覆盖第一个。
            合并成一个。
        """
        if not items:
            return "还没有任务"
        running = sum(1 for t in items if t.get("status") == "running")
        queued = sum(1 for t in items if t.get("status") in ("pending", "queued"))
        conc = prof.get("concurrency") or 1
        bits = [f"{running}/{conc} 在跑"]
        if queued:
            bits.append(f"{queued} 排队")
        return "  ·  ".join(bits)

    def _open_task(self, name: str) -> None:
        t = next((x for x in self.tasks if x.get("name") == name), None)
        if t:
            DetailDialog(t, self.api, self).exec()

    # ⚠️ 桌面上不再直接派活。
    #
    #    原来这里有个 _dispatch，把输入框里的内容发给 /api/dispatch。
    #    现在派活只走飞书 / QQ——用户直接跟丫头说。
    #
    #    为什么去掉桌面这条路：
    #        两条路都能派活，出了问题不知道是谁派的，
    #        也不知道该在哪边看回执。

