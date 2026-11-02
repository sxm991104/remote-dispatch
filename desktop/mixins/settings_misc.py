"""设置 · SettingsMiscMixin。

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

# ── 自绘控件 ──
# 这些是 desktop/widgets.py 里的自绘控件 ——
# 拆文件时漏了它们，窗口一开就 NameError。
from widgets import (
    PathRow,
)

from theme import (
    C_ACCENT, C_ACCENT2, C_ACCENT3, C_BAD, C_BG, C_DIM, C_DIM2,
    C_GRAD_A, C_GRAD_B, C_INPUT, C_LINE, C_LINE_HI, C_OK, C_RAISED,
    C_RUN, C_SURFACE, C_TEXT, C_TEXT2, C_WARN,
    STATUS_TEXT, _text_width, font, money, secs, status_color, toks,
)


class SettingsMiscMixin:

    def _card_autostart(self, lay: QVBoxLayout) -> None:
        """设置页 · 启动行为卡（开机自启）。"""
        # ── 启动行为 ──
        acard = QFrame()
        acard.setObjectName("card")
        al = QVBoxLayout(acard)
        al.setContentsMargins(20, 18, 20, 18)
        al.setSpacing(8)

        ah = QLabel("启动行为")
        ah.setFont(font(13, QFont.DemiBold))
        ah.setStyleSheet(f"color:{C_TEXT};")
        al.addWidget(ah)

        arow = QHBoxLayout()
        arow.setSpacing(12)

        self.switch_auto = QPushButton("开机自启")
        self.switch_auto.setCheckable(True)
        self.switch_auto.setCursor(Qt.PointingHandCursor)
        self.switch_auto.setMinimumWidth(110)
        self.switch_auto.clicked.connect(self._toggle_autostart)
        arow.addWidget(self.switch_auto)

        atip = QLabel("打开后，开机时自动把任务台启动起来。")
        atip.setFont(font(11))
        atip.setStyleSheet(f"color:{C_DIM2};")
        arow.addWidget(atip, 1)
        al.addLayout(arow)

        self.lbl_auto = QLabel("")
        self.lbl_auto.setFont(font(10.5))
        self.lbl_auto.setStyleSheet(f"color:{C_DIM2};")
        al.addWidget(self.lbl_auto)

        lay.addWidget(acard)


    def _card_paths(self, lay: QVBoxLayout) -> None:
        """设置页 · 路径配置卡（五条路径）。"""
        # ── 路径配置 ──
        # 这三个是启动服务必须知道的。原来写死在代码里，
        # 换台机器就跑不起来，所以搬到这里让用户自己管。
        pcard = QFrame()
        pcard.setObjectName("card")
        pl = QVBoxLayout(pcard)
        pl.setContentsMargins(20, 18, 20, 18)
        pl.setSpacing(12)

        ph = QLabel("程序装在哪")
        ph.setFont(font(13, QFont.DemiBold))
        ph.setStyleSheet(f"color:{C_TEXT};")
        pl.addWidget(ph)

        psub = QLabel("点「启动」的时候要用到这些。装好之后一般不用改。")
        psub.setFont(font(11))
        psub.setStyleSheet(f"color:{C_DIM2};")
        pl.addWidget(psub)

        self.path_rows = {}
        for key, label, hint, is_dir, name in (
            ("qq_exe", "QQ 程序", "比如 C:\\Program Files\\QQ.exe", False, "QQ.exe"),
            ("napcat_dir", "NapCat 目录", "里面有 NapCatWinBootMain.exe 的那个文件夹", True, ""),
            ("astrbot_exe", "对话服务程序", "astrbot.exe 的位置", False, "astrbot.exe"),
            ("astrbot_pyw", "Python 无窗口解释器", "pythonw.exe，没有它启动会闪一下黑框", False, "pythonw.exe"),
            ("astrbot_cwd", "对话服务数据目录", "AstrBot 的工作目录", True, ""),
        ):
            r = PathRow(key, label, hint, is_dir, name)
            self.path_rows[key] = r
            pl.addWidget(r)

        prow = QHBoxLayout()
        prow.setSpacing(8)

        b1 = QPushButton("重新探测")
        b1.setObjectName("ghost")
        b1.setCursor(Qt.PointingHandCursor)
        b1.clicked.connect(self._detect_paths)
        prow.addWidget(b1)

        b2 = QPushButton("保存")
        b2.setObjectName("primary")
        b2.setCursor(Qt.PointingHandCursor)
        b2.clicked.connect(self._save_paths)
        prow.addWidget(b2)

        self.lbl_paths = QLabel("")
        self.lbl_paths.setFont(font(10.5))
        self.lbl_paths.setStyleSheet(f"color:{C_DIM2};")
        prow.addWidget(self.lbl_paths, 1)
        pl.addLayout(prow)

        lay.addWidget(pcard)

    # ── 安全验证 ──

    def _detect_paths(self) -> None:
        """重新探测一遍路径，把结果填进输入框。

        注意：只填不保存。用户得点"保存"才算数——
        万一探测错了，不会把好用的配置覆盖掉。
        """
        def done(data):
            if not isinstance(data, dict) or not data.get("ok"):
                self.lbl_paths.setText("探测失败")
                return
            found = data.get("detected") or {}
            n = 0
            for k, row in self.path_rows.items():
                v = found.get(k) or ""
                if v:
                    row.set_value(v)
                    n += 1
            self.lbl_paths.setText(f"探测到 {n} 项，检查一下再保存")

        self.lbl_paths.setText("探测中…")
        self.api.request_once("/api/paths", callback=done)

    def _save_paths(self) -> None:
        body = {k: row.value() for k, row in self.path_rows.items()}

        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                miss = data.get("missing") or []
                if miss:
                    self.lbl_paths.setText(f"已保存，但还缺 {len(miss)} 项")
                else:
                    self.lbl_paths.setText("已保存")
            else:
                self.lbl_paths.setText("保存失败")

        self.api.request_once("/api/paths", body, callback=done)

    def _load_paths(self) -> None:
        """打开设置页时把当前路径填进去。"""
        def done(data):
            if not isinstance(data, dict) or not data.get("ok"):
                return
            vals = data.get("values") or {}
            for k, row in self.path_rows.items():
                row.set_value(vals.get(k) or "")
            miss = data.get("missing") or []
            if miss:
                names = [data.get("labels", {}).get(m, m) for m in miss]
                self.lbl_paths.setText("还缺：" + "、".join(names))
            else:
                self.lbl_paths.setText("")

        self.api.request_once("/api/paths", callback=done)

    def _refresh_autostart(self) -> None:
        """问一下现在是不是开机自启，刷新开关外观。"""
        def done(data):
            on = bool((data or {}).get("enabled"))
            self.switch_auto.setChecked(on)
            self.switch_auto.setObjectName("switchon" if on else "switchoff")
            self.switch_auto.setText("开机自启 · 开" if on else "开机自启 · 关")
            # objectName 变了要重新套一遍样式
            self.switch_auto.style().unpolish(self.switch_auto)
            self.switch_auto.style().polish(self.switch_auto)

        self.api.request_once("/api/autostart", callback=done)

    def _toggle_autostart(self) -> None:
        """点了开关：按当前状态翻转。

        ⚠️ 光靠 clicked 信号的默认行为不可靠：
            按钮是可 checkable 的，Qt 会自己翻转一次，
            如果这里再翻转一次就会来回抵消。
            所以统一以"服务端现在是什么状态"为准，取了再设成相反值。
        """
        want = not self.switch_auto.isChecked()

        def done(data):
            ok = bool((data or {}).get("ok"))
            if not ok:
                self.lbl_auto.setText((data or {}).get("error") or "设置失败")
            else:
                self.lbl_auto.setText((data or {}).get("note") or "")
            self._refresh_autostart()

        self.api.request_once("/api/autostart", {"on": want}, callback=done)

    def _load_model(self) -> None:
        self.api.get("/api/model")

    def _save_model(self) -> None:
        self.api.post("/api/model", {
            "preset": self.cmb_model.currentData(),
            "effort": self.cmb_effort.currentData(),
        })
        QTimer.singleShot(800, self._load_model)


# ══════════════════════════════════════════════════════
# 后台（接口服务）的拉起与回收
# ══════════════════════════════════════════════════════
#
# 为什么界面要自己拉后台：
#
#     实测：按 README 跑 python -m desktop.app 的人，
#     界面开了但接口服务没人起。于是界面上所有数据都是空的，
#     提示还写着「重开一下这个窗口」—— 重开一百次也一样。
#
#     一条命令就该能干完一件事。
#
# 为什么不裸起：
#
#     Windows 的进程**不连坐**。父进程退出，子进程照活。
#     用户关掉窗口，后台还在跑；下次开机再开一个 —— 越堆越多。
#     这就是「任务管理器里一堆 python.exe」的来源，
#     普通人看到会以为是病毒。
#
#     用 Job Object + KILL_ON_JOB_CLOSE：
#         把后台挂进 Job，界面进程一退出，句柄关闭，
#         系统自动把 Job 里所有进程杀干净。一步都不用我们做。

_BACKEND = None      # 我们拉起的那个后台（None 表示复用已有的）
_BACKEND_JOB = None


