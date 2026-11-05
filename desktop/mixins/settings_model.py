"""设置 · SettingsModelMixin。

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

import os

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
    C_ACCENT, C_ACCENT2, C_ACCENT3, C_ACCENT_TXT, C_BAD, C_BG, C_DIM, C_DIM2,
    C_GRAD_A, C_GRAD_B, C_INPUT, C_LINE, C_LINE_HI, C_OK, C_RAISED,
    C_RUN, C_SURFACE, C_TEXT, C_TEXT2, C_WARN,
    STATUS_TEXT, _text_width, font, money, secs, status_color, toks,
)


class SettingsModelMixin:

    def _card_security_mode(self, lay: QVBoxLayout) -> None:
        """设置页 · 安全模式。

        ⚠️ 这一张在最前面 —— 因为它决定了别的卡还生不生效。

            选了「出事可回退」，下面的验证码 / 审计员 / 守卫都不起作用，
            界面上得让用户看得见这个事实，而不是让他在别处瞎找。
        """
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 18, 20, 18)
        cl.setSpacing(10)

        h = QLabel("安全模式")
        h.setFont(font(13, QFont.DemiBold))
        h.setStyleSheet(f"color:{C_TEXT};")
        cl.addWidget(h)

        sub = QLabel("选一套就够，改了立刻生效，不用重启。")
        sub.setFont(font(11))
        sub.setStyleSheet(f"color:{C_DIM2};")
        sub.setWordWrap(True)
        cl.addWidget(sub)

        # 分段控件：三个选项在一行（照设计稿）
        #
        # ⚠️ 这里必须给 QFrame 一个 objectName，并且样式里带上选择器。
        #
        #    原来写的是 seg.setStyleSheet("background:...; border:...")——
        #    **没有选择器**的样式会套到这个 QFrame 的**所有子控件**上，
        #    包括里面那三个 QPushButton。结果：
        #        按钮自己的 chip:checked（黄色背景）被这层背景盖掉，
        #        三个选项永远看着一样，选了哪个完全看不出来。
        #
        #    实测（2026-11-05）：按钮 isChecked() 是 True，
        #    但截图量出来的像素还是 #121418（灰），不是 #e0a83c（黄）。
        seg = QFrame()
        seg.setObjectName("modeSeg")
        seg.setStyleSheet(
            f"QFrame#modeSeg {{ background:{C_INPUT};"
            f" border:1px solid {C_LINE}; border-radius:10px; }}"
        )
        segrow = QHBoxLayout(seg)
        segrow.setContentsMargins(4, 4, 4, 4)
        segrow.setSpacing(4)

        self.mode_btns = {}
        for key, title in (
            ("strict", "严格防护"),
            ("rollback", "出事可回退"),
            ("both", "全开"),
        ):
            b = QPushButton(title)
            # ⚠️ 不用全局的 #chip —— 那个依赖窗口级样式表，
            #    而这里父控件也有样式，容易互相盖。
            #    分段控件里的按钮单独一套 objectName，样式自己带全。
            b.setObjectName("modeBtn")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(30)
            b.clicked.connect(lambda _=False, k=key: self._set_security_mode(k))
            segrow.addWidget(b, 1)
            self.mode_btns[key] = b
        cl.addWidget(seg)

        # 分段按钮的样式：选中 = 暖琥珀（跟全站"选中"一个颜色）
        seg.setStyleSheet(
            seg.styleSheet()
            + f"""
            QPushButton#modeBtn {{
                background:transparent; border:1px solid transparent;
                color:{C_DIM}; padding:6px 14px; border-radius:8px;
                font-size:12.5px;
            }}
            QPushButton#modeBtn:hover {{
                color:{C_TEXT2}; background:{C_SURFACE};
            }}
            QPushButton#modeBtn:checked {{
                background:{C_ACCENT}; border-color:{C_ACCENT};
                color:{C_ACCENT_TXT}; font-weight:600;
            }}
            QPushButton#modeBtn:checked:hover {{
                background:{C_ACCENT2}; border-color:{C_ACCENT2};
            }}
            """
        )

        self.lbl_mode_now = QLabel(
            "严格 = 动手前先拦：动作守卫、影子执行、审计员、验证码、外传拦截。"
            "回退 = 不拦不码不要码，只做版本回退。"
            "全开 = 严格那几样全要，再加上回退。"
        )
        self.lbl_mode_now.setFont(font(10.5))
        self.lbl_mode_now.setStyleSheet(f"color:{C_DIM};")
        self.lbl_mode_now.setWordWrap(True)
        cl.addWidget(self.lbl_mode_now)

        lay.addWidget(card)

    def _load_security_mode(self) -> None:
        """拉安全模式。"""
        self.api.get("/api/security/mode")

    def _on_security_mode(self, d) -> None:
        if not isinstance(d, dict):
            return
        mode = d.get("mode") or "strict"

        # ── 先把三个按钮按"真实能力"标成可用/不可用 ──
        #
        # ⚠️ 为什么要这样：
        #
        #     老爸的原话（2026-11-05）：
        #         「邮箱没填比如关闭，应该要跳到出事可回退；
        #          如果影子执行、vss插件没有，有邮箱，那就只能走严格防护；
        #          如果都没有设置，就给提示，都开不了，只有配置了才能开。」
        #
        #     让用户点一个**实际做不到**的模式，等于给他虚假的安全感 ——
        #     以为有保护，其实没有。那还不如明说"现在没法开"。
        caps = None
        try:
            from guard import capabilities as _cap

            caps = _cap.capabilities()
        except Exception:  # noqa: BLE001
            caps = None

        names = {"strict": "严格防护", "rollback": "出事可回退", "both": "全开"}

        for k, b in getattr(self, "mode_btns", {}).items():
            b.setChecked(k == mode)
            if caps is None:
                b.setEnabled(True)
                b.setToolTip("")
                continue

            info = (caps.get("modes") or {}).get(k) or {}
            ok = bool(info.get("ok"))
            why = str(info.get("why") or "")

            if ok:
                b.setEnabled(True)
                b.setToolTip("")
                b.setStyleSheet("")
                continue

            # ⚠️ 已经选中的模式，条件不满足了 —— **不要假装它是好的。**
            #
            #    老爸实测（2026-11-05）：「我邮箱关了，还能切换到严格防护」。
            #
            #    为什么会这样：
            #        第一次写的是"当前模式强制 setEnabled(True)"，
            #        想着"用户已经选中了，禁用会让他不知道怎么办"。
            #        结果按钮是亮的、文字说"现在是严格防护"，
            #        但邮箱关着 —— 守卫拦下来根本发不出码，
            #        人就被卡死在那个状态里出不来。
            #
            #    第二次改成"保持能点 + 标警告色"，还是不灵：
            #        因为按钮此刻是 :checked，
            #        `QPushButton#modeBtn:checked` 那条样式**优先于**
            #        我单独设的 color —— 黄底还是黄底，一眼看不出有问题。
            #
            #    现在：**直接禁用**。
            #        禁用之后 Qt 会走 :disabled 样式（灰掉），
            #        一眼就知道"这个用不了"。
            #        鼠标悬停仍然能看到原因（tooltip 在禁用控件上也显示）。
            if k == mode:
                b.setEnabled(False)
                b.setToolTip(why)
                # 用 :checked:disabled 把选中的黄底也压掉，
                # 换成灰底 + 暗字，跟"不可用"一致。
                b.setStyleSheet(
                    f"QPushButton#modeBtn:checked:disabled {{"
                    f" background:{C_INPUT}; border-color:{C_LINE};"
                    f" color:{C_DIM}; font-weight:400; }}"
                )
            else:
                b.setEnabled(False)
                b.setToolTip(why)
                b.setStyleSheet("")

        f = d.get("features") or {}
        on = []
        if f.get("guard"):
            on.append("动作守卫")
        if f.get("shadow"):
            on.append("影子执行")
        if f.get("audit"):
            on.append("审计员")
        if f.get("code"):
            on.append("验证码")
        if f.get("exfil"):
            on.append("外传拦截")
        if f.get("rewind"):
            on.append("版本回退")

        line = (
            f"现在是「{d.get('label') or mode}」："
            f"{' + '.join(on) if on else '什么都不开'}"
        )

        # ── 服务端自动换过模式的话，把原因原样带出来 ──
        #
        # ⚠️ 这一段是为了让用户知道"发生了什么"。
        #
        #    场景：他本来开着严格防护，后来把邮箱关了。
        #        严格防护每拦一次都要发码，码发不出去 → 人卡死。
        #
        #    服务端读状态时会自动换成出事可回退，
        #    并回一个 why_changed 说明原因。
        #
        #    这儿不解释的话，用户会看到"怎么突然变成出事可回退了"，
        #    还以为有鬼。
        why_changed = str(d.get("why_changed") or "").strip()
        changed_from = str(d.get("changed_from") or "").strip()
        no_usable = bool(d.get("no_usable_mode"))

        if why_changed:
            line += "\n" + ("⚠️ " if (changed_from or no_usable) else "") + why_changed

        # ── 一个都开不了时，说清楚缺什么 ──
        if caps is not None:
            try:
                from guard import capabilities as _cap

                if not _cap.any_available(caps):
                    line += (
                        "\n⚠️ 现在一个模式都开不了："
                        "上面「版本回退」没启用、下面验证码邮箱也没配。"
                        "这两样至少得配一样。"
                    )
                else:
                    blocked = [
                        names[k] for k, v in (caps.get("modes") or {}).items()
                        if not v.get("ok")
                    ]
                    if blocked:
                        line += (
                            f"\n（{'、'.join(blocked)}暂时选不了 —— "
                            "悬停在那几个按钮上能看到缺什么。）"
                        )
            except Exception:  # noqa: BLE001
                pass

        self.lbl_mode_now.setText(line)

    def _set_security_mode(self, mode: str) -> None:
        """切安全模式。

        ⚠️ 切之前先查能力 —— 做不到的就不让切。

            按钮那边已经 disabled 了，但 disabled 可能被绕过
            （键盘、或者状态没刷新到），所以这里再拦一次。

            服务端也会拦（/api/security/mode 里查 capabilities），
            三层都拦一遍 —— 这种事宁可多拦。
        """
        try:
            from guard import capabilities as _cap

            caps = _cap.capabilities()
            info = (caps.get("modes") or {}).get(mode) or {}
            if not info.get("ok"):
                why = info.get("why") or "现在开不了这个模式"
                QMessageBox.warning(self, "安全模式", why)
                self._load_security_mode()
                return
        except Exception:  # noqa: BLE001
            # 查能力失败就不拦了，交给服务端判断
            pass

        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self._on_security_mode(data)
            else:
                self.lbl_mode_now.setText((data or {}).get("error") or "改不了")
                self._load_security_mode()

        self.api.request_once(
            "/api/security/mode", {"mode": mode}, timeout=20, callback=done
        )


    def _card_model(self, lay: QVBoxLayout) -> None:
        """设置页 · 任务端模型卡。原来是 _build_settings_page 里的一大段，按卡片边界切出来的。"""
        # ── 任务端模型 ──
        #
        # 做成"列表 + 详情"：
        #   上面是你的几套配置（每套一个服务商）
        #   点一行选中，下面填这一套的地址、Key、模型
        #   可以新增、删除、切换
        # ── 卡 1：模型列表（设计稿里是独立的一张卡，新增按钮在右上角） ──
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 18, 20, 18)
        cl.setSpacing(9)

        # 标题 + 副标题在左，新增按钮在右，同一行
        head = QHBoxLayout(); head.setSpacing(8)
        hcol = QVBoxLayout(); hcol.setSpacing(0)
        mh = QLabel("任务端模型")
        mh.setFont(font(13, QFont.DemiBold))
        mh.setStyleSheet(f"color:{C_TEXT};")
        msub = QLabel("存好几套，随时切换。当前用的那套会标绿点。")
        msub.setFont(font(11))
        msub.setStyleSheet(f"color:{C_DIM2};")
        hcol.addWidget(mh); hcol.addWidget(msub)
        head.addLayout(hcol, 1)

        self.btn_add_profile = QPushButton("＋ 新增模型")
        self.btn_add_profile.setObjectName("primary")
        self.btn_add_profile.setCursor(Qt.PointingHandCursor)
        self.btn_add_profile.clicked.connect(self._add_profile)
        head.addWidget(self.btn_add_profile, 0, Qt.AlignTop)
        cl.addLayout(head)

        # 配置列表。
        #
        # ⚠️ 必须包在一个 QWidget 里，不能直接 addLayout。
        #    直接塞 layout 的话，清空时 takeAt + deleteLater
        #    会作用到父容器身上，把别人的控件也删掉——
        #    实测报 "Internal C++ object already deleted"，
        #    整个设置页的输入框全废了。
        # ⚠️ **创建 QWidget 时就要给父控件。**
        #
        #    原来写的是 QWidget() —— 不带父控件，Qt 就把它当**独立顶层窗口**。
        #    虽然下一行 addWidget 会把它收养进去，但在"创建"到"被收养"
        #    之间的那几毫秒，屏幕上会闪出一个带系统标题栏
        #    （最小化/最大化/关闭三个按钮）的小窗口。
        #
        #    （2026-11-05 老爸一直在问"为啥切到模型页会闪个小窗口"。
        #      按住 20 张连拍、每 50ms 一张，才在第 3 张里抓到它。）
        #
        #    ⚠️ 不要用 setVisible(False) 来遮 ——
        #        Qt 里显式隐藏过的子控件，父容器 show 时它也不会显示，
        #        整个配置列表会直接消失。
        #        正确做法是**创建时就把父窗口传进去**：
        #        有父控件就从一开始不是顶层窗口，压根没机会闪。
        self.profile_host = QWidget(card)
        self.profile_box = QVBoxLayout(self.profile_host)
        self.profile_box.setContentsMargins(0, 0, 0, 0)
        self.profile_box.setSpacing(4)
        cl.addWidget(self.profile_host)
        lay.addWidget(card)

        # ── 卡 2：编辑区（设计稿里独立的"选一套来改"卡） ──
        card2 = QFrame()
        card2.setObjectName("card")
        cl = QVBoxLayout(card2)
        cl.setContentsMargins(20, 18, 20, 18)
        cl.setSpacing(9)

        self.edit_title = QLabel("选一套来改")
        self.edit_title.setFont(font(13, QFont.DemiBold))
        self.edit_title.setStyleSheet(f"color:{C_TEXT};")
        cl.addWidget(self.edit_title)

        # 接口地址 + 模型名 并排（照设计稿）
        toprow = QHBoxLayout()
        toprow.setSpacing(12)

        base_col = QVBoxLayout(); base_col.setSpacing(5)
        lb_base = QLabel("接口地址")
        lb_base.setFont(font(11)); lb_base.setStyleSheet(f"color:{C_DIM2};")
        base_col.addWidget(lb_base)
        self.ed_base = QLineEdit()
        self.ed_base.setMinimumHeight(36)
        self.ed_base.setPlaceholderText("https://api.openai.com")
        base_col.addWidget(self.ed_base)
        toprow.addLayout(base_col, 1)

        model_col = QVBoxLayout(); model_col.setSpacing(5)
        lbl_model = QLabel("模型名")
        lbl_model.setFont(font(11)); lbl_model.setStyleSheet(f"color:{C_DIM2};")
        model_col.addWidget(lbl_model)
        self.cmb_engine_model = QComboBox()
        self.cmb_engine_model.setEditable(False)
        self.cmb_engine_model.setMinimumHeight(36)
        self.cmb_engine_model.setCursor(Qt.PointingHandCursor)
        model_col.addWidget(self.cmb_engine_model)
        toprow.addLayout(model_col, 1)

        cl.addLayout(toprow)

        # API Key（整行）
        lb_key = QLabel("API Key")
        lb_key.setFont(font(11)); lb_key.setStyleSheet(f"color:{C_DIM2};")
        cl.addWidget(lb_key)
        self.ed_key = QLineEdit()
        self.ed_key.setMinimumHeight(36)
        self.ed_key.setPlaceholderText("留空表示不改")
        self.ed_key.setEchoMode(QLineEdit.Password)
        # 填完 Key 自动拉一次模型，不用再点"测试连接"。
        self.ed_key.editingFinished.connect(
            lambda: self._fetch_models(silent=True)
        )
        cl.addWidget(self.ed_key)

        self.lbl_key_state = QLabel("")
        self.lbl_key_state.setFont(font(10.5))
        self.lbl_key_state.setStyleSheet(f"color:{C_DIM2};")
        cl.addWidget(self.lbl_key_state)

        # 快速填：只在「新增模型」时出现。
        #
        # 为什么点已有模型时不显示：
        #     那套的地址和模型早就填好了，
        #     旁边还摆一排"一键填某家"的按钮，纯属干扰。
        self.quick_row_host = QWidget(card2)
        quick = QHBoxLayout(self.quick_row_host)
        quick.setContentsMargins(0, 0, 0, 0)
        quick.setSpacing(8)
        ql = QLabel("快速填：")
        ql.setFont(font(10.5))
        ql.setStyleSheet(f"color:{C_DIM2};")
        quick.addWidget(ql)
        # ⚠️ 这里的模型名只是"先填个最常见的"，不一定是你账号下的真实名字。
        #
        #    以前写的是 deepseek-chat——那是旧名字，
        #    DeepSeek 现在返回的是 deepseek-flash / deepseek-v4-pro。
        #    点了快速填之后名字对不上，还得手动改。
        #
        #    所以：快速填只负责把**地址**填对，模型名那个下拉框
        #    交给 _fetch_models 拉真实列表（界面一打开就自动拉）。
        #    填进去的模型名只是个占位，列表一到就会被替换掉。
        for name, base, model in (
            ("DeepSeek", "https://api.deepseek.com", ""),
            ("OpenAI", "https://api.openai.com/v1", ""),
            ("Kimi", "https://api.moonshot.cn", ""),
            ("通义", "https://dashscope.aliyuncs.com/compatible-mode/v1", ""),
            ("硅基流动", "https://api.siliconflow.cn/v1", ""),
        ):
            b = QPushButton(name)
            b.setObjectName("ghost")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(
                lambda _=False, u=base: (
                    self.ed_base.setText(u),
                    self._fetch_models(silent=True),
                )
            )
            quick.addWidget(b)
        quick.addStretch()
        cl.addWidget(self.quick_row_host)

        # 操作按钮：同样只在「新增模型」时出现。
        self.action_row_host = QWidget(card2)
        arow2 = QHBoxLayout(self.action_row_host)
        arow2.setContentsMargins(0, 0, 0, 0)
        arow2.setSpacing(8)

        b_test = QPushButton("测试连接")
        b_test.setObjectName("ghost")
        b_test.setCursor(Qt.PointingHandCursor)
        b_test.clicked.connect(self._test_engine)
        arow2.addWidget(b_test)

        b_save = QPushButton("保存")
        b_save.setObjectName("primary")
        b_save.setCursor(Qt.PointingHandCursor)
        b_save.clicked.connect(self._save_engine)
        arow2.addWidget(b_save)

        # 「删除这套」拿掉了——上面每一行已经有「删」按钮，
        # 这里再放一个，等于同一个动作两个入口。
        # 保留一个属性名，别的地方还在用（禁用/启用的状态控制）。
        #
        # ⚠️ **创建时就要给父窗口。**
        #
        #    原来写的是：
        #        self.btn_del_profile = QPushButton("删除这套")
        #        self.btn_del_profile.hide()
        #
        #    QPushButton() 不带父控件 = **独立顶层窗口**，
        #    而且 Qt 给无父控件的是默认 640x480。
        #    在这两行之间那一瞬间，屏幕上会闪出一个 640x480 的
        #    带系统标题栏的窗口。
        #
        #    （2026-11-05 老爸反复问"切到模型页为啥闪个小窗口"。
        #      用 app.topLevelWidgets() 一查抓到的：
        #        QPushButton 640x480 可见=False
        #      640x480 正是无父控件的默认尺寸，一对就对上了。）
        self.btn_del_profile = QPushButton("删除这套", card2)
        self.btn_del_profile.hide()

        self.lbl_engine = QLabel("")
        self.lbl_engine.setFont(font(10.5))
        self.lbl_engine.setStyleSheet(f"color:{C_DIM2};")
        self.lbl_engine.setWordWrap(True)
        arow2.addWidget(self.lbl_engine, 1)
        cl.addWidget(self.action_row_host)

        self.lbl_engine_now = QLabel("")
        self.lbl_engine_now.setFont(font(10.5))
        self.lbl_engine_now.setStyleSheet(f"color:{C_DIM2};")
        cl.addWidget(self.lbl_engine_now)

        # 卡 2（编辑区）加进 lay。卡 1 已经在前面加过了。
        lay.addWidget(card2)


    def _load_engine(self) -> None:
        """把当前的模型配置读出来填进界面。key 不显示原文。"""
        def done(data):
            if not isinstance(data, dict) or not data.get("ok"):
                return
            self.ed_base.setText(data.get("base_url") or "")
            self._set_model_text(data.get("model") or "")
            if data.get("has_key"):
                # placeholder 要短。
                # 原来写"已设置（尾号 40e0），留空表示不改"，21 个字，
                # 在 32px 高的输入框里被截断，用户只看到前半截，
                # 以为这儿没值。提示信息挪到下面那行状态标签里。
                self.ed_key.setPlaceholderText(f"已保存 · 尾号 {data.get('key_tail')}")
                self.lbl_key_state.setText(
                    f"✓ 已有 Key（尾号 {data.get('key_tail')}）  要换就输入新的，留空表示不改"
                )
                self.lbl_key_state.setStyleSheet(f"color:{C_OK}; font-size:10.5px;")
                self.lbl_engine_now.setText(
                    f"正在用 {data.get('model')}"
                )
            else:
                self.ed_key.setPlaceholderText("还没填")
                self.lbl_key_state.setText("✗ 还没填 Key")
                self.lbl_key_state.setStyleSheet(f"color:{C_BAD}; font-size:10.5px;")
                self.lbl_engine_now.setText("还没配。填完才能派任务。")

        self.api.request_once("/api/engine", callback=done)

    def _model_text(self) -> str:
        """当前选的模型 —— 返回的是 id，不是界面上显示的那个名字。

        比如界面显示 DeepSeek-V4.1-Flash，
        这里返回 deepseek-flash（调接口必须用这个）。
        """
        data = self.cmb_engine_model.currentData()
        if isinstance(data, str) and data.strip():
            return data.strip()
        return self.cmb_engine_model.currentText().strip()

    def _set_model_text(self, name: str) -> None:
        """把模型名放进下拉框。

        下拉框是不可编辑的（为了长得像下拉框）。
        但配置里存的模型名可能不在当前列表里——
        比如列表还没拉到、或者用户手填过一个冷门名字。

        这种情况下临时加一项进去，保证界面上显示的还是真实值。
        """
        if not name:
            return
        # 先按 id 找（正常情况：列表是接口拉的，id 对得上）
        idx = self.cmb_engine_model.findData(name)
        if idx < 0:
            # 再按显示名找（配置里存的就是显示名的情况）
            idx = self.cmb_engine_model.findText(name)
        if idx >= 0:
            self.cmb_engine_model.setCurrentIndex(idx)
        else:
            # 都不在 → 插一项进去，显示和值都用这个名字。
            # 不 setEditText（不可编辑的下拉框没有 lineEdit）。
            self.cmb_engine_model.insertItem(0, name, name)
            self.cmb_engine_model.setCurrentIndex(0)

    def _fetch_models(self, silent: bool = False) -> None:
        """去接口问有哪些模型，填进下拉框。

        什么时候调：
            · 点开一套配置时**自动**调（silent=True，不刷状态栏）
            · 点「测试连接」时再调一次

        为什么自动调：
            用户的原话——"我现在 deepseek 已经有 key 了啊，
            不应该直接是下拉框？"
            对。Key 都在了还要先点个按钮才给下拉框，是多余的。

        silent 是什么意思：
            自动调的时候不要抢状态栏那行字——
            用户没点任何东西，屏幕上的字自己在跳，会以为出问题了。
        """
        base = self.ed_base.text().strip()
        key = self.ed_key.text().strip()

        if not base:
            self.lbl_engine.setText("先填接口地址")
            return

        # ⚠️ Key 是空的**不等于没填**。
        #
        #    已经保存过的 Key 不回显（只显示"已保存 · 尾号 xxxx"），
        #    所以 ed_key 是空的。
        #    这时要把 id 一起传过去，让后端用存着的那把 Key。
        #
        #    原来这里写的是 `if not base or not key: return`——
        #    等于凡是"已经存过 Key"的配置，点测试都静默什么都不做。
        body = {"base_url": base}
        if key:
            body["api_key"] = key
        else:
            pid = getattr(self, "_selected_pid", "")
            if not pid:
                self.lbl_engine.setText("先把 Key 填上")
                return
            body["id"] = pid

        if not silent:
            self.lbl_engine.setText("正在查可用模型…")

        # 临时日志：查清楚这条链路走到哪一步断了
        try:
            _log = os.path.join(os.path.expanduser("~"), "Downloads", "dsh_debug.log")
            with open(_log, "a", encoding="utf-8") as _f:
                _f.write(f"[fetch_models] body={body}\n")
        except Exception:
            pass

        def done(data):
            try:
                _log = os.path.join(os.path.expanduser("~"), "Downloads", "dsh_debug.log")
                with open(_log, "a", encoding="utf-8") as _f:
                    _f.write(f"[fetch_models] resp={str(data)[:300]}\n")
            except Exception:
                pass
            if not isinstance(data, dict) or not data.get("ok"):
                # 查不到就让用户手打，不纠缠。
                # 自动调的时候不吭声——用户没点任何东西，
                # 突然冒一句"查不到"会以为出错了。
                if not silent:
                    self.lbl_engine.setText("查不到模型列表，手动填模型名吧")
                return
            ids = data.get("ids") or data.get("models") or []
            labels = data.get("labels") or ids
            cur = self._model_text()

            self.cmb_engine_model.blockSignals(True)
            self.cmb_engine_model.clear()
            for mid, label in zip(ids, labels):
                # 显示 label，背后挂 id。
                # 官方 DeepSeek 就是这样：显示 DeepSeek-V4.1-Flash，
                # 实际调用要用 deepseek-flash。
                self.cmb_engine_model.addItem(label, mid)
            self.cmb_engine_model.blockSignals(False)

            # 现在用的模型如果在列表里就选它，否则用接口推荐的默认值
            pick = cur if cur in ids else (data.get("default") or "")
            if pick:
                self._set_model_text(pick)

            if not silent:
                self.lbl_engine.setText(f"查到 {len(ids)} 个模型")

        self.api.request_once(
            "/api/engine/models",
            body,
            timeout=40, callback=done,
        )

    def _suggest_name(self, base_url: str) -> str:
        """按接口地址给个能认出来的名字。

        ⚠️ 名字不能跟已有的重——重了列表里分不清。
           所以撞名就加序号：Kimi、Kimi 2、Kimi 3。
        """
        low = (base_url or "").lower()
        base = ""
        for k, v in (
            ("deepseek", "DeepSeek"),
            ("moonshot", "Kimi"),
            ("kimi", "Kimi"),
            ("openai", "OpenAI"),
            ("dashscope", "通义"),
            ("siliconflow", "硅基流动"),
            ("bigmodel", "智谱"),
            ("baichuan", "百川"),
        ):
            if k in low:
                base = v
                break
        if not base:
            try:
                host = low.split("//", 1)[-1].split("/")[0]
                base = host.split(".")[0] or "新模型"
            except Exception:  # noqa: BLE001
                base = "新模型"

        # 撞名就加序号
        taken = {
            (x.get("name") or "").strip().lower()
            for x in (self._profiles or [])
        }
        if base.lower() not in taken:
            return base
        i = 2
        while f"{base} {i}".lower() in taken:
            i += 1
        return f"{base} {i}"

    def _save_engine(self) -> None:
        """保存。

        两种情况：
            正在新增  → action=add，把表单内容一起提交，建一条新的
            在看已有的 → action=update，改那一套
        """
        adding = getattr(self, "_is_adding", False)
        pid = getattr(self, "_selected_pid", "")

        if not adding and not pid:
            self.lbl_engine.setText("先点「＋ 新增模型」")
            return

        base_url = self.ed_base.text().strip()
        model = self._model_text()
        api_key = self.ed_key.text().strip()

        if not base_url:
            self.lbl_engine.setText("接口地址得填")
            return
        if not model:
            self.lbl_engine.setText("模型名得填（等列表出来选一个）")
            return
        if adding and not api_key:
            self.lbl_engine.setText("新模型得填 Key")
            return

        if adding:
            # 新增：一次把表单内容全交上去，后端建一条新的。
            # 名字先按地址给个默认，用户之后可以改。
            body = {
                "action": "add",
                "name": self._suggest_name(base_url),
                "base_url": base_url,
                "model": model,
                "api_key": api_key,
                "enabled": True,
            }
        else:
            body = {
                "action": "update",
                "id": pid,
                "base_url": base_url,
                "model": model,
            }
            # Key 留空表示"不改"——已经存过的 Key 不回显，
            # 用户不重打一遍就是不想换。
            if api_key:
                body["api_key"] = api_key

        def done(data):
            # ⚠️ 不管成功失败，**立刻清空 Key 框**。
            #
            #    跟审计员那张卡一个道理（2026-11-05 老爸抓到的那次）：
            #        原来只有"保存成功"才 clear()。
            #        保存失败 / 请求超时 → Key 一直留在框里。
            #
            #    虽然 EchoMode.Password 挡住字形，但框里的内容还是明文，
            #    Ctrl+A / Ctrl+C 一复制就拿到完整 Key。
            #
            #    用户按「保存」那一刻，Key 已经赋给 body 了，留在界面上
            #    只有泄露风险，没有任何好处。
            try:
                self.ed_key.clear()
            except Exception:  # noqa: BLE001
                pass

            if isinstance(data, dict) and data.get("ok"):
                self.lbl_engine.setText("已保存")
                self._is_adding = False
                self._set_new_mode(False)
                # 记下刚建的那套，免得刷新时挑错
                # 后端 add 返回的是 {"ok": True, "id": "p123"}——
                # 不要在 data["profile"] 里找。
                newid = data.get("id") or ""
                if newid:
                    self._selected_pid = newid
                self._load_profiles()
            else:
                self.lbl_engine.setText(
                    "保存失败：{}".format((data or {}).get("error") or "未知原因")
                )

        self.lbl_engine.setText("保存中…")
        self.api.request_once("/api/engine/profiles", body, callback=done)

    def _test_engine(self) -> None:
        """真的发一次请求，确认这套配置能用。

        顺手拉一遍模型列表：如果用户填的模型名在列表里没有，
        说明名字写错了，当场就能看出来，不用等任务报 404。
        """
        self._fetch_models()
        _k = self.ed_key.text().strip()
        body = {
            "base_url": self.ed_base.text().strip(),
            "model": self._model_text(),
        }
        # Key 空着不等于没填——已保存的 Key 不回显。
        # 这时传 id，让后端用存着的那把。
        if _k:
            body["api_key"] = _k
        else:
            _pid = getattr(self, "_selected_pid", "")
            if _pid:
                body["id"] = _pid
        self.lbl_engine.setText("正在连…")

        def done(data):
            if not isinstance(data, dict):
                self.lbl_engine.setText("没收到回应")
                return
            if data.get("ok"):
                self.lbl_engine.setText(data.get("message") or "通了")
            else:
                self.lbl_engine.setText(data.get("message") or data.get("error") or "不通")

        self.api.request_once("/api/engine/test", body, timeout=60, callback=done)

    def _fill_editor(self, prof: dict) -> None:
        """把某一套填进下面的编辑区。"""
        # 标题保持设计稿的「选一套来改」，不随选中项变（选中项在上面列表里高亮）。
        self.edit_title.setText("选一套来改")
        self.ed_base.setText(prof.get("base_url") or "")
        self._set_model_text(prof.get("model") or "")

        # 下拉框里先放当前模型，等「测试连接」再拉全量
        self.cmb_engine_model.clear()
        if prof.get("model"):
            self.cmb_engine_model.addItem(prof["model"], prof["model"])

        # ⚠️ 填 KEY 和地址的时候别把上一步拉到的模型列表清掉。
        #
        #    用户点「测试连接」→ 下拉框里变成十几个模型 →
        #    然后点另一套配置 → 又变回一个。
        #    这是对的：每套配置的模型列表不一样。
        self._models_loaded_for = ""
        self.cmb_engine_model.setCurrentIndex(0)

        if prof.get("has_key"):
            self.ed_key.clear()
            self.ed_key.setPlaceholderText(f"已保存 · 尾号 {prof.get('key_tail')}")
            self.lbl_key_state.setText(
                f"✓ 已有 Key（尾号 {prof.get('key_tail')}）  要换就输入新的"
            )
            self.lbl_key_state.setStyleSheet(f"color:{C_OK}; font-size:10.5px;")
            self.lbl_engine_now.setText(
                f"正在改：{prof.get('name')}｜{prof.get('model')}"
            )
        else:
            self.ed_key.clear()
            self.ed_key.setPlaceholderText("还没填")
            self.lbl_key_state.setText("✗ 这套还没填 Key")
            self.lbl_key_state.setStyleSheet(f"color:{C_BAD}; font-size:10.5px;")
            self.lbl_engine_now.setText(f"正在改：{prof.get('name')}")

        self.btn_del_profile.setEnabled(True)

        # 点的是已有的一套 → 隐藏"新增才用"的那些控件。
        self._set_new_mode(False)

        # 填完自动拉一遍模型列表。
        #
        # 用 singleShot 推到当前事件循环之外：
        #    _fill_editor 是在"点行"和"刷新"两个地方调的，
        #    直接发请求会在信号处理里嵌套一次网络调用，
        #    极端情况下界面会卡一下。
        #    推后一拍执行，界面先画出来再拉。
        from PySide6.QtCore import QTimer as _QT

        _QT.singleShot(50, lambda: self._fetch_models(silent=True))

