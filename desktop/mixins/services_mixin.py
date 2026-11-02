"""设置 · ServicesPageMixin。

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

# LoginQRDialog / ServiceMonitor 在 desktop/svc_flow.py 里。
# ⚠️ 拆文件时漏了这一行 —— 窗口能开，但一点「启动」就 NameError。
from svc_flow import LoginQRDialog, ServiceMonitor

# ── Qt ──
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
    ServiceCard,
)

from theme import (
    C_ACCENT, C_ACCENT2, C_ACCENT3, C_BAD, C_BG, C_DIM, C_DIM2,
    C_GRAD_A, C_GRAD_B, C_INPUT, C_LINE, C_LINE_HI, C_OK, C_RAISED,
    C_RUN, C_SURFACE, C_TEXT, C_TEXT2, C_WARN,
    STATUS_TEXT, _text_width, font, money, secs, status_color, toks,
)


class ServicesPageMixin:

    def _build_services_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(14)

        h = QLabel("服务")
        h.setFont(font(19, QFont.Bold))
        h.setStyleSheet(f"color:{C_TEXT};")
        sub = QLabel("用哪个开哪个，没开的不会启动。")
        sub.setFont(font(12))
        sub.setStyleSheet(f"color:{C_DIM};")
        lay.addWidget(h)
        lay.addWidget(sub)

        # ⚠️ 这三样拆开是为了省内存：
        #
        #        QQ   → 拉 QQ 客户端（几百兆）
        #        飞书 → 只是网络连接（几十兆）
        #        任务端 → 跟通道无关，自己一份
        #
        #    只用飞书的人不该白背着 QQ。
        #
        # 布局用 tab 而不是三张卡片竖排：
        #    竖排时一屏挤三个，每个都被压扁，说明文字挤成一团。
        #    tab 一次只看一个，空间够，读得清。
        self.svc_cards = {}

        self.svc_tabs = QTabWidget()
        self.svc_tabs.setObjectName("svctabs")
        self.svc_tabs.setStyleSheet(
            "QTabWidget::pane {"
            f" border:1px solid {C_LINE}; border-radius:10px;"
            f" background:{C_SURFACE}; top:-1px;"
            "}"
            "QTabBar::tab {"
            f" background:transparent; color:{C_DIM};"
            " padding:8px 18px; margin-right:4px;"
            f" border-bottom:2px solid transparent;"
            "}"
            "QTabBar::tab:hover {"
            f" color:{C_TEXT2};"
            "}"
            "QTabBar::tab:selected {"
            f" color:{C_TEXT}; border-bottom:2px solid {C_ACCENT};"
            "}"
        )

        for key, title, desc in (
            # ⚠️ 顺序和措辞有讲究：
            #
            #    官方接口的平台排前面（飞书、以后还有钉钉之类），
            #    QQ 排后面，而且标题里带「社区方案」——
            #
            #    因为 QQ 那条路靠 NapCat 往 QQ 进程注入 DLL，
            #    依赖客户端内部结构、条款上是灰区。
            #    用户该知道这两类的差别，而不是看成一排等价的选择。
            ("feishu", "飞书通道", "官方接口，纯网络连接，很轻，几秒钟就能开"),
            ("qq", "QQ 通道（社区方案）",
             "靠 NapCat 注入 QQ 客户端，占内存；依赖 QQ 版本，有风控风险"),
            ("bridge", "任务端", "真正在电脑上干活的那个，跟通道无关"),
        ):
            card = ServiceCard(key, title, desc)
            card.action_clicked.connect(self._svc_action)
            self.svc_cards[key] = card

            # 每张卡外面包一层留边距——不然贴着 tab 边很难看
            wrap = QWidget()
            wl = QVBoxLayout(wrap)
            wl.setContentsMargins(16, 16, 16, 16)
            wl.addWidget(card)
            wl.addStretch()
            self.svc_tabs.addTab(wrap, title)

        lay.addWidget(self.svc_tabs, 1)

        # 一行提示：说明为什么拆开
        hint = QLabel(
            "只需要一个平台的话，只开那一个就行——另一个不会启动，省内存。"
        )
        hint.setFont(font(11))
        hint.setStyleSheet(f"color:{C_DIM2};")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        # ⚠️ 模型分组搬到「任务」页了。
        #
        #    这里原来也放了一份，等于同一个东西画两遍——
        #    而且服务页的职责是"开关服务"，不是"看模型在跑什么"。
        #
        #    现在：服务页只管三件事启停，模型状态去任务页看。

        lay.addStretch()
        return page

    # ── 设置页 ──
    def _on_qq_stopped(self) -> None:
        """用户放弃扫码，QQ 已经停掉。界面回到干净状态。"""
        card = self.svc_cards.get("qq")
        if card:
            card.clear_working()
            card.set_ok(False)
        self.pills["qq"].set_color(C_BAD)

    def _on_services(self, d: dict) -> None:
        """服务状态变了。按钮和顶栏都跟着刷新。

        每个组件自己判断"有没有变"，没变就不重绘。
        这一层不额外做判断——状态逻辑放组件里，别两处都管。

        ⚠️ 只处理**真的返回了的字段**。
            原来写 bool(d.get(k))，字段缺失时 .get 返回 None，
            bool(None) 是 False——于是"这次没问到"被当成了"这个关了"。

            现在的规则：这个键不在返回里，说明这次没问到它，
            保留上一次的显示，什么都别动。
        """
        for k, dot in self.pills.items():
            if k not in d:
                continue
            dot.set_color(C_OK if d.get(k) else C_BAD)
        for k, card in self.svc_cards.items():
            if k not in d:
                continue
            card.set_ok(bool(d.get(k)))

    def _svc_action(self, key: str, action: str) -> None:
        """启动或重启一个服务。

        流程设计（这一版的关键改动）：
            不在"请求返回"上等结果。请求只是把动作发出去，
            真正的进度靠 ServiceMonitor 每 1.5 秒问一次服务状态。
            这样：
                · 用户中途点「我先不管」，按钮不会卡住
                · QQ 要扫码等一分钟，界面照样能动
                · 状态一真起来，按钮自己和二维码窗口自己收尾
        """
        names = {"qq": "QQ 通道", "feishu": "飞书通道", "bridge": "任务端"}
        verb = "启动" if action == "start" else "重启"

        card = self.svc_cards.get(key)
        if card:
            card.mark_working()

        # 飞书通道不需要扫码，直接走。
        # 它比 QQ 轻得多：宿主起来了、把这条连接打开就行，几秒钟的事。
        # 所以不走下面那套"等扫码"的流程。

        # QQ 起的时候把扫码头摆出来。
        # 它自己会判断：一旦发现 QQ 通了就关掉自己，
        # 所以不需要提前猜"这次要不要扫"。
        if key == "qq" and LoginQRDialog is not None:
            if self._qr_dlg is None or not self._qr_dlg.isVisible():
                self._qr_dlg = LoginQRDialog(self)
                # 用户点「不管了」→ QQ 已经被停掉，
                # 按钮和状态都要跟着回到"没启动"
                self._qr_dlg.stopped.connect(self._on_qq_stopped)
                self._qr_dlg.show()

        path = "/api/start" if action == "start" else "/api/restart"
        label = f"{verb}{names[key]}"

        def done(data):
            if not isinstance(data, dict):
                if card:
                    card.clear_working()
                return
            if data.get("need_login"):
                # 要扫码：把窗口叫到前面来，别让用户自己找
                if self._qr_dlg is not None:
                    self._qr_dlg.raise_()
                    self._qr_dlg.activateWindow()
                return
            if not data.get("ok") and data.get("error"):
                # 只在失败时弹窗。成功不打扰——界面自己会变成"重启"
                QMessageBox.warning(self, f"{label}没成", str(data["error"]))
            if card:
                card.clear_working()

        # 超时分开给：
        #   QQ   → 要等扫码，给足 200 秒
        #   飞书 → 只是连个网络，120 秒足够（含 AstrBot 冷启动）
        #   任务端 → 立刻就回
        timeout = {"qq": 200, "feishu": 150, "bridge": 30}.get(key, 120)
        self.api.request_once(path, {"target": key},
                              timeout=timeout, callback=done)

