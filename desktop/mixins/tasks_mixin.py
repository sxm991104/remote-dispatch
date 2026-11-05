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
        page.setObjectName("pagebg")
        page.setStyleSheet(f"QWidget#pagebg {{ background:{C_BG}; }}")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(16)

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

        # 筛选胶囊放在 tab 栏右边（设计稿：左 tab 右筛选同一行）
        self.chip_btns = []
        chip_host = QWidget()
        chip_row = QHBoxLayout(chip_host)
        chip_row.setContentsMargins(0, 0, 0, 0)
        chip_row.setSpacing(6)
        for ckey, ctext in (("all", "全部"), ("running", "运行中"),
                            ("ok", "已完成"), ("bad", "失败")):
            c = QPushButton(ctext)
            c.setObjectName("chip")
            c.setCheckable(True)
            c.setChecked(ckey == self.filter)
            c.setCursor(Qt.PointingHandCursor)
            c.clicked.connect(lambda _=False, k=ckey: self._set_filter(k))
            chip_row.addWidget(c)
            self.chip_btns.append((ckey, c))
        self.model_tabs.setCornerWidget(chip_host, Qt.TopRightCorner)

        # 每个 tab 的内容是一个滚动区，装这个模型名下的任务行
        self.task_tab_host = QWidget()
        self.task_tab_host_lay = QVBoxLayout(self.task_tab_host)
        self.task_tab_host_lay.setContentsMargins(0, 0, 0, 0)
        self.task_tab_host_lay.setSpacing(8)

        lay.addWidget(self.model_tabs, 1)

        # 没配模型 / 没任务时的空态（平时藏起来，有数据时由 _render_tasks 切换）
        self._tasks_empty = QLabel(
            "还没有配置模型。去「模型」页加一套，任务才知道交给谁跑。"
        )
        self._tasks_empty.setFont(font(11.5))
        self._tasks_empty.setWordWrap(True)
        self._tasks_empty.setStyleSheet(
            f"color:{C_DIM2}; background:{C_SURFACE}; border-radius:12px;"
            " padding:28px 24px;"
        )
        self._tasks_empty.setVisible(False)
        lay.addWidget(self._tasks_empty, 1)

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

        # ⚠️ 先把任务身上的 profile 归一成模型 id。
        #
        #    任务里存的值有**两种格式**，混在一起的：
        #
        #        新任务    p1793460955495      ← 模型 id
        #        老任务    deepseek / kimi    ← 模型名（小写）
        #
        #    而 tab 是按模型 id 建的。不归一的话，
        #    老任务（37 个 kimi + 28 个 deepseek）哪个 tab 都进不去 ——
        #    表现就是"Kimi 那个 tab 是空的，任务全堆在第一个 tab 里"。
        #
        #    实测（2026-11-05）：老爸切到 kimi 的 tab 再点状态筛选，
        #    跳回第一个；而且 deepseek 下面显示着一堆 kimi 的活。
        def _norm(t: dict) -> str:
            raw = str(t.get("profile") or "").strip()
            if not raw:
                return ""
            # 已经是对得上的 id，直接用
            for p in profs:
                if raw == (p.get("id") or ""):
                    return raw
            # 不然按名字对（不分大小写，去掉空格和横线）
            key = raw.lower().replace(" ", "").replace("-", "").replace("_", "")
            for p in profs:
                nm = str(p.get("name") or "").lower().replace(" ", "")
                nm = nm.replace("-", "").replace("_", "")
                if nm and nm == key:
                    return p.get("id") or ""
            # 对不上就归到"认不出来的"，别悄悄塞进第一个 tab
            return ""

        # 筛选
        shown = tasks
        if self.filter == "running":
            shown = [t for t in tasks if t.get("status") == "running"]
        elif self.filter == "ok":
            shown = [t for t in tasks if t.get("status") == "ok"]
        elif self.filter == "bad":
            shown = [t for t in tasks if t.get("status") in ("error", "timeout")]

        groups: dict = {}
        orphan = 0
        for t in shown:
            pid = _norm(t)
            if not pid:
                orphan += 1
                continue
            groups.setdefault(pid, []).append(t)

        order = {"running": 0, "pending": 1, "queued": 1}
        for k in groups:
            groups[k] = sorted(
                groups[k], key=lambda t: order.get(t.get("status"), 2)
            )

        # 没有配置时给一句明确提示
        if not profs:
            self._clear_tabs()
            self.model_tabs.setVisible(False)
            self._tasks_empty.setVisible(True)
            self._render_sig = ("noprof",)
            return
        # 有配置：把空态藏起来，显示 tab 区
        self._tasks_empty.setVisible(False)
        self.model_tabs.setVisible(True)

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
            return

        # ── 结构变了：重建 ──
        #
        # ⚠️ 重建之前先记住"现在选的是哪个模型"。
        #
        #    原来是直接 _clear_tabs() 再从头 addTab，
        #    重建完 tab 默认停在第 0 个。
        #
        #    于是：切到 Kimi 的 tab → 点一下状态筛选（触发重建）
        #          → **跳回第一个模型**。
        #
        #    实测（2026-11-05）：老爸切到备用 Kimi 再点状态，
        #    每次都弹回去，看着像筛选把 tab 重置了。
        #
        #    这里按模型 id 记，不按序号 —— 模型增删之后序号会变。
        try:
            _keep = self.model_tabs.currentIndex()
            _keep_pid = ""
            _blocks_before = getattr(self, "_task_blocks", {}) or {}
            for _pid, _b in _blocks_before.items():
                if _b.get("index") == _keep:
                    _keep_pid = _pid
                    break
        except Exception:  # noqa: BLE001
            _keep_pid = ""

        self._clear_tabs()
        self._task_blocks = {}
        lay = self.task_tab_host.layout()

        for tab_index, p in enumerate(profs):
            pid = p.get("id") or ""
            items = groups.get(pid, [])[:self._list_limit]

            page = QWidget()
            page.setObjectName("tabpage")
            page.setStyleSheet(f"QWidget#tabpage {{ background:{C_BG}; }}")
            pl = QVBoxLayout(page)
            pl.setContentsMargins(4, 10, 4, 4)
            pl.setSpacing(10)

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

            # 每套模型一个 tab。第一套是主力，后面是备用，照设计稿加前缀。
            role = "主力" if tab_index == 0 else "备用"
            pname = p.get("name") or "未命名"
            idx = self.model_tabs.addTab(scroll, f"{role}·{pname}")
            self._task_blocks[pid] = {
                "rows": rows, "index": idx,
            }

        # 恢复到重建前选中的那个模型。
        #
        # ⚠️ 找不到就留在第 0 个（比如那个模型被删了）——
        #    这比瞎猜一个好。
        if _keep_pid:
            try:
                for _pid, _b in self._task_blocks.items():
                    if _pid == _keep_pid:
                        self.model_tabs.setCurrentIndex(_b["index"])
                        break
            except Exception:  # noqa: BLE001
                pass

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

