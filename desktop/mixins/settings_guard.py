"""设置 · SettingsGuardMixin。

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

from theme import (
    C_ACCENT, C_ACCENT2, C_ACCENT3, C_BAD, C_BG, C_DIM, C_DIM2,
    C_GRAD_A, C_GRAD_B, C_INPUT, C_LINE, C_LINE_HI, C_OK, C_RAISED,
    C_RUN, C_SURFACE, C_TEXT, C_TEXT2, C_WARN,
    STATUS_TEXT, _text_width, font, money, secs, status_color, toks,
)


class SettingsGuardMixin:

    def _card_guard(self, lay: QVBoxLayout) -> None:
        """设置页 · 安全验证卡（收验证码的邮箱、发信服务器、授权码）。"""
        # ── 安全验证 ──
        #
        # 危险操作（删库、回滚、读密钥）要验证码。
        # 前两次是提醒和警告，第三次才发码到邮箱。
        #
        # 为什么用邮箱不用短信：
        #   短信要服务商、要钱、要备案。邮箱免费、独立、
        #   而且同样"不在对话里"——这才是关键。
        scard = QFrame()
        scard.setObjectName("card")
        sc = QVBoxLayout(scard)
        sc.setContentsMargins(20, 18, 20, 18)
        sc.setSpacing(9)

        sh = QLabel("危险操作验证")
        sh.setFont(font(13, QFont.DemiBold))
        sh.setStyleSheet(f"color:{C_TEXT};")
        sc.addWidget(sh)

        ssub = QLabel(
            "删库、回滚代码、读密钥这类操作，会发验证码到你邮箱。"
            "前两次只是提醒和警告，第三次坚持才发码。"
        )
        ssub.setFont(font(11))
        ssub.setStyleSheet(f"color:{C_DIM2};")
        ssub.setWordWrap(True)
        sc.addWidget(ssub)

        # 开关
        srow = QHBoxLayout()
        srow.setSpacing(10)
        self.btn_guard = QPushButton("开启")
        self.btn_guard.setObjectName("ghost")
        self.btn_guard.setCursor(Qt.PointingHandCursor)
        self.btn_guard.clicked.connect(self._toggle_guard)
        srow.addWidget(self.btn_guard)
        self.lbl_guard_state = QLabel("")
        self.lbl_guard_state.setFont(font(10.5))
        self.lbl_guard_state.setStyleSheet(f"color:{C_DIM2};")
        srow.addWidget(self.lbl_guard_state, 1)
        sc.addLayout(srow)

        def sfield(label: str, placeholder: str, secret: bool = False):
            lb = QLabel(label)
            lb.setFont(font(11))
            lb.setStyleSheet(f"color:{C_DIM2};")
            sc.addWidget(lb)
            ed = QLineEdit()
            ed.setMinimumHeight(36)
            ed.setPlaceholderText(placeholder)
            if secret:
                ed.setEchoMode(QLineEdit.Password)
            sc.addWidget(ed)
            return ed

        self.ed_guard_email = sfield("收验证码的邮箱", "you@example.com")

        # 服务器不让人填——填完邮箱自动认出来。
        #
        # 让用户去查"你的邮箱 SMTP 服务器是什么"，是个很烦的门槛：
        # 大部分人根本不知道这个词，得去搜文档，搜到了还怕填错。
        # 常见邮箱就那么几家，认一下就完事。
        lbl_host = QLabel("发信服务器")
        lbl_host.setFont(font(11))
        lbl_host.setStyleSheet(f"color:{C_DIM2};")
        sc.addWidget(lbl_host)

        self.lbl_guard_host = QLabel("填上面的邮箱，自动认")
        self.lbl_guard_host.setFont(font(11.5))
        self.lbl_guard_host.setStyleSheet(f"color:{C_DIM2};")
        self.lbl_guard_host.setMinimumHeight(36)
        self.lbl_guard_host.setStyleSheet(
            f"color:{C_DIM2}; padding:8px 13px;"
            f" background:{C_INPUT}; border:1px solid {C_LINE}; border-radius:9px;"
        )
        sc.addWidget(self.lbl_guard_host)

        # 填完邮箱立刻认
        self.ed_guard_email.textChanged.connect(self._detect_guard_host)

        # 发信账号要单独填。
        #
        # 为什么不能默认用收件邮箱：
        #   收件邮箱是"发到哪"，发信账号是"谁发的"。
        #   这俩经常不一样——比如你收件用 163，
        #   但机器人的发信账号是专门注册的一个 QQ 邮箱。
        self.ed_guard_user = sfield(
            "前台 Agent 发信账号", "机器人用哪个邮箱发给你"
        )
        self.ed_guard_pass = sfield(
            "前台 Agent 发信授权码", "这个账号的授权码，不是登录密码",
            secret=True,
        )

        grow = QHBoxLayout()
        grow.setSpacing(8)
        g1 = QPushButton("保存")
        g1.setObjectName("primary")
        g1.setCursor(Qt.PointingHandCursor)
        g1.clicked.connect(self._save_guard)
        grow.addWidget(g1)

        g2 = QPushButton("发封测试邮件")
        g2.setObjectName("ghost")
        g2.setCursor(Qt.PointingHandCursor)
        g2.clicked.connect(self._test_guard)
        grow.addWidget(g2)

        self.lbl_guard_msg = QLabel("")
        self.lbl_guard_msg.setFont(font(10.5))
        self.lbl_guard_msg.setStyleSheet(f"color:{C_DIM2};")
        self.lbl_guard_msg.setWordWrap(True)
        grow.addWidget(self.lbl_guard_msg, 1)
        sc.addLayout(grow)

        ghint = QLabel(
            "常见配置：QQ 邮箱 smtp.qq.com｜163 邮箱 smtp.163.com｜"
            "Gmail smtp.gmail.com"
        )
        ghint.setFont(font(10))
        ghint.setStyleSheet(f"color:{C_DIM2};")
        ghint.setWordWrap(True)
        sc.addWidget(ghint)

        # ⚠️ 验证码输入框。
        #
        #    原来这块只有一句说明，**没有输入框、没有按钮**。
        #    而锁死之后唯一的出路就是填一个验证码——
        #    也就是说：状态机走到死路，用户自己出不来。
        #
        #    现在补上。填完点「解锁」，调 /api/guard/unlock。
        crow = QHBoxLayout()
        crow.setSpacing(8)

        self.ed_unlock_code = QLineEdit()
        self.ed_unlock_code.setPlaceholderText("收到邮件里的 6 位验证码")
        self.ed_unlock_code.setMaxLength(12)
        self.ed_unlock_code.setMinimumWidth(160)
        crow.addWidget(self.ed_unlock_code)

        b_unlock = QPushButton("解锁")
        b_unlock.setObjectName("primary")
        b_unlock.setCursor(Qt.PointingHandCursor)
        b_unlock.clicked.connect(self._do_unlock)
        crow.addWidget(b_unlock)
        crow.addStretch()
        sc.addLayout(crow)

        self.lbl_unlock_msg = QLabel("")
        self.lbl_unlock_msg.setFont(font(10.5))
        self.lbl_unlock_msg.setStyleSheet(f"color:{C_DIM2};")
        self.lbl_unlock_msg.setWordWrap(True)
        sc.addWidget(self.lbl_unlock_msg)

        lay.addWidget(scard)


    def _do_unlock(self) -> None:
        """填验证码解锁。

        ⚠️ 这个方法曾经**只有调用没有定义**：
            行 2103 写了 b_unlock.clicked.connect(self._do_unlock)，
            而 _do_unlock 不存在。

            后果不是「点了没反应」，是**整个桌面端起不来** ——
                AttributeError: 'MainWindow' object has no attribute '_do_unlock'
            因为 connect 是在 _card_guard 里执行的，
            而 _card_guard 在 __init__ 里，窗口还没画出来就崩了。

            实测：全新 clone + 全新用户目录，python -m desktop.app
            退出码 1，连个窗口都没有。

        py_compile 检查不出这类问题（运行期才报），
        所以只能**真的启动一次**才看得见。
        """
        code = self.ed_unlock_code.text().strip()
        if not code:
            self.lbl_unlock_msg.setText("先把邮件里的验证码填上")
            return

        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self.lbl_unlock_msg.setText(data.get("message") or "解开了")
                self.ed_unlock_code.clear()
                # 解开之后守卫状态变了，把卡片刷一遍
                self.api.get("/api/guard")
            else:
                self.lbl_unlock_msg.setText(
                    (data or {}).get("message")
                    or (data or {}).get("error")
                    or "码不对，或者过期了"
                )

        self.lbl_unlock_msg.setText("验证中…")
        self.api.request_once(
            "/api/guard/unlock", {"code": code}, timeout=30, callback=done
        )

    def _load_guard(self) -> None:
        """把安全邮箱配置读出来。"""

        def done(data):
            if not isinstance(data, dict) or not data.get("ok"):
                return
            self._guard_on = bool(data.get("enabled"))
            self.ed_guard_email.setText(data.get("email") or "")
            self.ed_guard_user.setText(data.get("smtp_user") or "")
            # 服务器不用填，_detect_guard_host 会跟着邮箱自动更新
            self.ed_guard_pass.clear()

            if data.get("has_pass"):
                self.ed_guard_pass.setPlaceholderText("已保存 · 留空表示不改")
            else:
                self.ed_guard_pass.setPlaceholderText(
                    "邮箱设置里那个「授权码」，不是登录密码"
                )

            self._refresh_guard_ui(bool(data.get("ready")))

        self.api.request_once("/api/guard", callback=done)

    def _refresh_guard_ui(self, ready: bool) -> None:
        on = getattr(self, "_guard_on", False)
        self.btn_guard.setText("开启" if not on else "关闭")
        if not ready:
            text = "还没配好 · 危险操作目前不验证"
            color = C_BAD
        elif on:
            text = "已开启 · 危险操作要邮箱验证码"
            color = C_OK
        else:
            text = "配置好了，但开关是关的"
            color = C_WARN
        self.lbl_guard_state.setText(text)
        self.lbl_guard_state.setStyleSheet(f"color:{color}; font-size:10.5px;")

    def _toggle_guard(self) -> None:
        on = not getattr(self, "_guard_on", False)
        self._guard_on = on

        def done(_d) -> None:
            self._load_guard()
            # ⚠️ 开关一变，安全模式能不能选就跟着变 —— **必须重拉**。
            #
            #    老爸实测（2026-11-05）：
            #        「我关掉邮箱，立刻点击严格防护，是可以点击的，
            #          但是过了一会儿不行；开启后也一样，延迟很高。」
            #
            #    两层延迟叠在一起：
            #        ① 服务端 capabilities 缓存 30 秒
            #           （那个在 /api/guard 处理时清掉了）
            #        ② **桌面端根本没重新拉安全模式** ——
            #           它还拿着切页时拿到的那份旧数据在画按钮
            #
            #    第二层是主因：不清数据源，清多少缓存都没用。
            self._refresh_security_mode()

        self.api.request_once("/api/guard", {"enabled": on}, callback=done)

    def _refresh_security_mode(self) -> None:
        """重新拉一次安全模式（连带能力判断），刷新三个按钮。"""
        try:
            self.api.get("/api/security/mode")
        except Exception:  # noqa: BLE001
            pass

    # 常见邮箱的 SMTP 服务器
    _SMTP = {
        "qq.com": ("smtp.qq.com", 465),
        "foxmail.com": ("smtp.qq.com", 465),
        "163.com": ("smtp.163.com", 465),
        "126.com": ("smtp.126.com", 465),
        "yeah.net": ("smtp.yeah.net", 465),
        "sina.com": ("smtp.sina.com", 465),
        "sohu.com": ("smtp.sohu.com", 465),
        "aliyun.com": ("smtp.aliyun.com", 465),
        "139.com": ("smtp.139.com", 465),
        "gmail.com": ("smtp.gmail.com", 465),
        "outlook.com": ("smtp-mail.outlook.com", 587),
        "hotmail.com": ("smtp-mail.outlook.com", 587),
        "live.com": ("smtp-mail.outlook.com", 587),
        "yahoo.com": ("smtp.mail.yahoo.com", 465),
        "icloud.com": ("smtp.mail.me.com", 587),
    }

    def _detect_guard_host(self, text: str) -> None:
        """填邮箱时自动显示会用的发信服务器。"""
        text = (text or "").strip().lower()
        if "@" not in text:
            self.lbl_guard_host.setText("填上面的邮箱，自动认")
            self.lbl_guard_host.setStyleSheet(
                f"color:{C_DIM2}; padding:8px 13px;"
                f" background:{C_INPUT}; border:1px solid {C_LINE};"
                f" border-radius:9px;"
            )
            self._guard_host_value = ("", 0)
            return

        domain = text.rsplit("@", 1)[1]

        # 先查表
        host, port = self._SMTP.get(domain, ("", 0))
        guessed = False

        if not host:
            # ⚠️ 表里没有不等于配不了。
            #
            #    原来这里直接置空，_save_guard 看到空值就 return——
            #    用户填了真实邮箱，界面只说一句"认不出"，
            #    实际上什么都没提交，而屏幕上写着"发送失败"
            #    （那是上一次的残留），用户一头雾水。
            #
            #    现在按通用规律猜：xxx@company.com → smtp.company.com
            #    企业邮箱绝大多数是这个规律。猜错了用户能改。
            host = f"smtp.{domain}"
            port = 465
            guessed = True

        if guessed:
            self.lbl_guard_host.setText(
                f"{host}  端口 {port}   ← 按域名推测的，不对就手动改"
            )
            self.lbl_guard_host.setStyleSheet(
                f"color:{C_WARN}; padding:8px 13px;"
                f" background:{C_INPUT}; border:1px solid {C_LINE};"
                f" border-radius:9px;"
            )
        else:
            self.lbl_guard_host.setText(f"{host}  端口 {port}   ← 自动识别")
            self.lbl_guard_host.setStyleSheet(
                f"color:{C_OK}; padding:8px 13px;"
                f" background:{C_INPUT}; border:1px solid {C_LINE};"
                f" border-radius:9px;"
            )

        self._guard_host_value = (host, port)

    def _save_guard(self) -> None:
        host, port = getattr(self, "_guard_host_value", ("", 0))
        body = {
            "email": self.ed_guard_email.text().strip(),
            "smtp_host": host,
            "smtp_port": port,
            "smtp_user": self.ed_guard_user.text().strip(),
            "smtp_pass": self.ed_guard_pass.text().strip(),
            "enabled": True,
        }
        if not body["email"]:
            self.lbl_guard_msg.setText("收验证码的邮箱得填")
            return
        # host 为空只可能是检测函数还没跑过（没输邮箱）。
        # 那时也不该静默 return——说清楚缺什么。
        if not host:
            self.lbl_guard_msg.setText("先把上面的邮箱填上")
            return
        if not body["smtp_user"]:
            self.lbl_guard_msg.setText("前台 Agent 发信账号得填")
            return

        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self.lbl_guard_msg.setText("已保存")
                self._guard_on = True
                self._load_guard()
                # 保存完邮箱，安全模式能不能选可能就变了 —— 重拉一次
                self._refresh_security_mode()
            else:
                self.lbl_guard_msg.setText("保存失败")

        self.lbl_guard_msg.setText("保存中…")
        self.api.request_once("/api/guard", body, callback=done)

    def _test_guard(self) -> None:
        """发一封测试邮件。

        ⚠️ 把**界面上填的值**一起发过去。
        #
        #    原来发的是空 body，服务端只好去读已保存的配置——
        #    而用户还没点保存（正常顺序是先测再存）。
        #    结果：用户填了 163 邮箱，测的却是 test@example.com，
        #    永远"发送失败"，而他还以为是自己填错了。
        """
        host, port = getattr(self, "_guard_host_value", ("", 0))
        body = {
            "email": self.ed_guard_email.text().strip(),
            "smtp_host": host,
            "smtp_port": port,
            "smtp_user": self.ed_guard_user.text().strip(),
            "smtp_pass": self.ed_guard_pass.text().strip(),
        }
        if not body["email"]:
            self.lbl_guard_msg.setText("先把收验证码的邮箱填上")
            return

        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self.lbl_guard_msg.setText(data.get("message") or "发出去了")
            else:
                self.lbl_guard_msg.setText(
                    (data or {}).get("message")
                    or (data or {}).get("error")
                    or "发不出去"
                )

        self.lbl_guard_msg.setText("发测试邮件…")
        self.api.request_once("/api/guard/test", body, timeout=40, callback=done)

    # ── 行为 ──
