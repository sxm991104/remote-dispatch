"""设置 · SettingsAuditorMixin。

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

from api import API
from theme import (
    C_ACCENT, C_ACCENT2, C_ACCENT3, C_BAD, C_BG, C_DIM, C_DIM2,
    C_GRAD_A, C_GRAD_B, C_INPUT, C_LINE, C_LINE_HI, C_OK, C_RAISED,
    C_RUN, C_SURFACE, C_TEXT, C_TEXT2, C_WARN,
    STATUS_TEXT, _text_width, font, money, secs, status_color, toks,
)


class SettingsAuditorMixin:

    def _card_auditor(self, lay: QVBoxLayout) -> None:
        """设置页 · 安全审核员。

        ⚠️ 交互跟「任务端用哪个模型」那张卡一样：

            接口地址  →  填 Key  →  **模型名变成下拉框**

            不能是三个输入框死板地排着 ——
            用户填完 Key 就该看到可选模型，不用盲打。

            （原来我写的是三个纯输入框，用户看出来是"输入"不是"选"，
              这里返工。）
        """
        acard = QFrame()
        acard.setObjectName("card")
        al = QVBoxLayout(acard)
        al.setContentsMargins(20, 18, 20, 18)
        al.setSpacing(9)

        ah = QLabel("安全审核员")
        ah.setFont(font(13, QFont.DemiBold))
        ah.setStyleSheet(f"color:{C_TEXT};")
        al.addWidget(ah)

        asub = QLabel(
            "另开一个模型，专门看「这次改动对不对」。"
            "它不能对话、没有记忆、不读项目 —— 所以骗不了它。"
        )
        asub.setFont(font(11))
        asub.setStyleSheet(f"color:{C_DIM2};")
        asub.setWordWrap(True)
        al.addWidget(asub)

        # ── 开关 ──
        arow = QHBoxLayout()
        arow.setSpacing(12)

        self.switch_auditor = QPushButton("启用审核")
        self.switch_auditor.setCheckable(True)
        self.switch_auditor.setCursor(Qt.PointingHandCursor)
        self.switch_auditor.clicked.connect(self._toggle_auditor)
        arow.addWidget(self.switch_auditor)

        self.lbl_auditor_state = QLabel("")
        self.lbl_auditor_state.setFont(font(10.5))
        self.lbl_auditor_state.setStyleSheet(f"color:{C_DIM2};")
        arow.addWidget(self.lbl_auditor_state, 1)
        al.addLayout(arow)

        # ── 接口地址 ──
        lb1 = QLabel("接口地址")
        lb1.setFont(font(11))
        lb1.setStyleSheet(f"color:{C_DIM2};")
        al.addWidget(lb1)

        # ⚠️ 输入框在快速填**上面**：
        #    标签 → 输入框 → 快速填按钮。
        #    原来我把按钮插在标签和输入框之间，读起来是
        #        「接口地址」
        #        [DeepSeek] [OpenAI] …
        #        [输入框]
        #    而「快速填」这三个字又是贴在按钮前面的，
        #    整体就跳脱了，看着像两行不相关的东西。
        self.ed_auditor_url = QLineEdit()
        self.ed_auditor_url.setMinimumHeight(36)
        self.ed_auditor_url.setPlaceholderText("点下面那排就行，不用手敲")
        al.addWidget(self.ed_auditor_url)

        # 快速填：点一下就填好地址，然后自动去拉模型列表。
        #
        #    用户不可能记得住
        #    https://dashscope.aliyuncs.com/compatible-mode/v1 这种东西，
        #    敲错一个字母就是「测试连不上」。
        #
        #    跟「任务端用哪个模型」那张卡同一套按钮、同一个做法：
        #    点一下把地址填好，然后自动去拉模型列表。
        aq = QHBoxLayout()
        aq.setContentsMargins(0, 0, 0, 0)
        aq.setSpacing(8)
        aql = QLabel("快速填：")
        aql.setFont(font(10.5))
        aql.setStyleSheet(f"color:{C_DIM2};")
        aq.addWidget(aql)
        for name, base in (
            ("DeepSeek", "https://api.deepseek.com"),
            ("OpenAI", "https://api.openai.com/v1"),
            ("Kimi", "https://api.moonshot.cn"),
            ("通义", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            ("硅基流动", "https://api.siliconflow.cn/v1"),
        ):
            b = QPushButton(name)
            b.setObjectName("ghost")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(
                lambda _=False, u=base: (
                    self.ed_auditor_url.setText(u),
                    self._fetch_auditor_models(silent=True),
                )
            )
            aq.addWidget(b)
        aq.addStretch()
        al.addLayout(aq)



        # ── API Key ──
        lb2 = QLabel("API Key")
        lb2.setFont(font(11))
        lb2.setStyleSheet(f"color:{C_DIM2};")
        al.addWidget(lb2)

        self.ed_auditor_key = QLineEdit()
        self.ed_auditor_key.setMinimumHeight(36)
        self.ed_auditor_key.setPlaceholderText("审核员自己的 key（可以跟主模型不同）")
        self.ed_auditor_key.setEchoMode(QLineEdit.Password)
        al.addWidget(self.ed_auditor_key)

        # 填完 Key 自动拉一次模型 —— 跟主模型那张卡一个行为。
        self.ed_auditor_key.editingFinished.connect(
            lambda: self._fetch_auditor_models(silent=True)
        )

        self.lbl_auditor_key_state = QLabel("")
        self.lbl_auditor_key_state.setFont(font(10.5))
        self.lbl_auditor_key_state.setStyleSheet(f"color:{C_DIM2};")
        al.addWidget(self.lbl_auditor_key_state)

        # ── 模型名：下拉框，不是输入框 ──
        #
        # ⚠️ setEditable(False) —— 点一下就是列表。
        #
        #    editable 那个版本长得跟输入框一模一样，
        #    用户看不出这里有东西可选，只能盲打（打错一个字母就 404）。
        #
        #    列表里没有的（自建 / 代理）→ 最后一项「手动输入…」。
        lb3 = QLabel("模型名")
        lb3.setFont(font(11))
        lb3.setStyleSheet(f"color:{C_DIM2};")
        al.addWidget(lb3)

        self.cmb_auditor_model = QComboBox()
        self.cmb_auditor_model.setEditable(False)
        self.cmb_auditor_model.setMinimumHeight(36)
        self.cmb_auditor_model.setCursor(Qt.PointingHandCursor)
        self.cmb_auditor_model.addItem("（先填 Key，会自动列出可用模型）")
        al.addWidget(self.cmb_auditor_model)

        # 手动输入的那个框（默认藏起来，选了「手动输入…」才露出来）
        self.ed_auditor_model_manual = QLineEdit()
        self.ed_auditor_model_manual.setMinimumHeight(36)
        self.ed_auditor_model_manual.setPlaceholderText("自己填模型名")
        self.ed_auditor_model_manual.hide()
        al.addWidget(self.ed_auditor_model_manual)

        self.cmb_auditor_model.currentTextChanged.connect(
            self._on_auditor_model_changed
        )

        # ── 底部按钮 ──
        abrow = QHBoxLayout()
        abrow.setSpacing(8)

        # ⚠️ 跟主模型那张卡一样，这里是「测试连接」不是「拉模型列表」。
        #
        #    测试连接会**顺手拉一遍模型列表** ——
        #    如果填的模型名在列表里没有，说明名字写错了，当场就能看出来。
        #
        #    （我原来写成「拉模型列表」，少了一个更重要的动作：
        #      它不验证这套 Key 到底能不能用。）
        b_test = QPushButton("测试连接")
        b_test.setObjectName("ghost")
        b_test.setCursor(Qt.PointingHandCursor)
        b_test.clicked.connect(self._test_auditor)
        abrow.addWidget(b_test)

        b1 = QPushButton("保存")
        b1.setObjectName("primary")
        b1.setCursor(Qt.PointingHandCursor)
        b1.clicked.connect(self._save_auditor)
        abrow.addWidget(b1)

        self.lbl_auditor_msg = QLabel("")
        self.lbl_auditor_msg.setFont(font(10.5))
        self.lbl_auditor_msg.setStyleSheet(f"color:{C_DIM2};")
        self.lbl_auditor_msg.setWordWrap(True)
        abrow.addWidget(self.lbl_auditor_msg, 1)
        al.addLayout(abrow)

        lay.addWidget(acard)

    def _fetch_auditor_models(self, silent: bool = False) -> None:
        """去接口问审核员那套 Key 能看到哪些模型，填进下拉框。

        跟主模型的 _fetch_models 一个套路，只是用审核员自己的地址和 Key。
        """
        base = self.ed_auditor_url.text().strip()
        key = self.ed_auditor_key.text().strip()

        if not base:
            if not silent:
                self.lbl_auditor_msg.setText("先填接口地址")
            return

        # Key 是空的**不等于没填** —— 可能是存过的（不回显）。
        body = {"base_url": base}
        if key:
            body["api_key"] = key
        else:
            # 存过的 Key 在 auditor.json 里，由服务端自己读
            body["use_saved"] = True

        if not silent:
            self.lbl_auditor_msg.setText("正在查可用模型…")

        def done(data):
            if not isinstance(data, dict) or not data.get("ok"):
                if not silent:
                    self.lbl_auditor_msg.setText(
                        (data or {}).get("error") or "查不到模型列表"
                    )
                return

            models = data.get("models") or []
            cur = self.cmb_auditor_model.currentText()

            self.cmb_auditor_model.blockSignals(True)
            self.cmb_auditor_model.clear()
            for m in models:
                self.cmb_auditor_model.addItem(str(m))
            self.cmb_auditor_model.addItem("手动输入…")
            self.cmb_auditor_model.blockSignals(False)

            # 尽量保住原来选的那个
            if cur:
                i = self.cmb_auditor_model.findText(cur)
                if i >= 0:
                    self.cmb_auditor_model.setCurrentIndex(i)

            if not silent:
                self.lbl_auditor_msg.setText(f"查到 {len(models)} 个模型")

        self.api.request_once("/api/engine/models", body, timeout=40, callback=done)

    def _on_auditor_model_changed(self, text: str) -> None:
        """选了「手动输入…」就把输入框露出来。"""
        if text == "手动输入…":
            self.ed_auditor_model_manual.show()
            self.ed_auditor_model_manual.setFocus()
        else:
            self.ed_auditor_model_manual.hide()

    def _load_auditor(self) -> None:
        """读审核员配置，填进界面。"""
        self.api.get("/api/auditor")

    def _on_auditor(self, d) -> None:
        if not isinstance(d, dict):
            return
        self.switch_auditor.setChecked(bool(d.get("enabled")))
        self.ed_auditor_url.setText(d.get("base_url") or "")
        self.ed_auditor_key.setText(d.get("api_key") or "")

        # 模型名先塞进下拉框（哪怕列表还没拉）。
        #
        # ⚠️ 不能直接 setCurrentText —— 非可编辑的 QComboBox 里
        #    没有这个字符串时 settCurrentText 是空操作，
        #    用户看到的是空白，以为配置丢了。
        name = d.get("model") or ""
        if name:
            i = self.cmb_auditor_model.findText(name)
            if i < 0:
                self.cmb_auditor_model.blockSignals(True)
                self.cmb_auditor_model.insertItem(0, name)
                self.cmb_auditor_model.blockSignals(False)
                i = 0
            self.cmb_auditor_model.setCurrentIndex(i)

        ready = d.get("_ready")
        why = d.get("_why") or ""
        if ready:
            self.lbl_auditor_state.setText("已在用")
            self.lbl_auditor_state.setStyleSheet(f"color:{C_OK};")
        else:
            self.lbl_auditor_state.setText(why or "没开")
            self.lbl_auditor_state.setStyleSheet(f"color:{C_DIM2};")

    def _toggle_auditor(self) -> None:
        on = self.switch_auditor.isChecked()

        def done(_d=None):
            self.lbl_auditor_msg.setText("打开了" if on else "关掉了")
            self._load_auditor()

        self.api.request_once(
            "/api/auditor", {"enabled": on}, timeout=20, callback=done
        )

    def _test_auditor(self) -> None:
        """真的发一次请求，确认审核员这套配置能用。

        顺手拉一遍模型列表：如果填的模型名在列表里没有，
        说明名字写错了，当场就能看出来。

        跟主模型的 _test_engine 一个套路，只是用审核员自己的地址和 Key。
        """
        # 先拉模型（失败也不影响后面的测试）
        self._fetch_auditor_models(silent=True)

        base = self.ed_auditor_url.text().strip()
        key = self.ed_auditor_key.text().strip()

        sel = self.cmb_auditor_model.currentText()
        if sel == "手动输入…":
            sel = self.ed_auditor_model_manual.text().strip()

        if not base:
            self.lbl_auditor_msg.setText("先填接口地址")
            return

        body = {"base_url": base}
        if key:
            body["api_key"] = key
        if sel:
            body["model"] = sel

        self.lbl_auditor_msg.setText("正在连…")

        def done(data):
            if not isinstance(data, dict):
                self.lbl_auditor_msg.setText("没收到回应")
                return
            if data.get("ok"):
                self.lbl_auditor_msg.setText(data.get("message") or "通了")
            else:
                self.lbl_auditor_msg.setText(
                    data.get("message") or data.get("error") or "不通"
                )

        self.api.request_once("/api/engine/test", body, timeout=60, callback=done)

    def _save_auditor(self) -> None:
        # 模型名：下拉框选的，或者手动输入的
        sel = self.cmb_auditor_model.currentText()
        if sel == "手动输入…":
            sel = self.ed_auditor_model_manual.text().strip()

        body = {
            "base_url": self.ed_auditor_url.text().strip(),
            "api_key": self.ed_auditor_key.text().strip(),
            "model": sel,
        }
        if not all(body.values()):
            self.lbl_auditor_msg.setText("三样都要填")
            return

        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self.lbl_auditor_msg.setText(
                    "存好了" + ("" if data.get("ready") else f"（{data.get('why') or ''}）")
                )
            else:
                self.lbl_auditor_msg.setText(
                    (data or {}).get("error") or "存不进去"
                )

        self.lbl_auditor_msg.setText("保存中…")
        self.api.request_once("/api/auditor", body, timeout=30, callback=done)


