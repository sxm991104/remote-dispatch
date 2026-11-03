"""dsh-bridge 桌面端。

界面定位
    给不会配置的人用的一个 Windows 程序。核心诉求是"看着舒服、敢信"，
    不是"功能堆满"。

配色思路
    纯白刺眼，纯黑压抑，取中间：
    · 底色用偏暖的深灰蓝，不是死黑，长时间看不会累
    · 卡片靠明度和圆角分层，不靠粗边框
    · 主色是低饱和的紫蓝，只在真正要动手的地方出现
    · 状态色降低饱和度，做成柔和的胶囊

设计上守住的
    · 只连桥的 HTTP 接口，不碰 AstrBot —— 换掉 AstrBot 时这里一行不用改
    · 后台线程做网络请求，界面永远不卡
    · 出错说人话，不甩异常

启动
    python desktop_qt.py
"""

from __future__ import annotations

import os
import sys

# ⚠️ 把自己所在的仓库根目录塞进 sys.path。
#
#    这个文件现在住在 desktop/ 里，而它要 import 的模块
#    （tools.deps、guard.* 等）在仓库根下面。
#
#    从 desktop/app.py 启动时，sys.path[0] 是 desktop/，
#    找不到 tools 这个包——环境页因此一片空白。
#
#    实测：加了这一行，环境页才正常显示。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 配色和小工具已经拆到 desktop/theme.py —— 那头没有任何项目内部依赖，
# 是界面这一层的最底层。
# 接口层拆到 desktop/api.py —— 那层只管"发请求、拿结果、回调"，
# 跟界面无关，也零项目内部依赖（除了 tools.userdirs 这个独立的路径工具）。
# 控件拆到 desktop/widgets.py —— 跟"哪一页"无关的那些自绘小东西。
# 设置页 / 任务页 / 环境页 / 服务页拆到 desktop/mixins/ 下了。
#
# 用 mixin 而不是"独立 widget 类"的原因见各 mixin 文件的开头。
# 详情弹窗拆到 desktop/dialogs.py —— mixin 要用它，但它不能反过来依赖 app.py。

from mixins.deps_mixin import DepsPageMixin
from mixins.services_mixin import ServicesPageMixin
from mixins.settings_auditor import SettingsAuditorMixin
from mixins.settings_guard import SettingsGuardMixin
from mixins.settings_misc import SettingsMiscMixin
from mixins.settings_model import SettingsModelMixin
from mixins.settings_profiles import SettingsProfilesMixin
from mixins.tasks_mixin import TasksPageMixin

from widgets import (
    BusyIndicator,
    GlowDot,
    GradientBar,
    PathRow,
    ServiceCard,
    StatusBadge,
    TaskRow,
)

from api import API, Api, _API_PORT

from theme import (
    C_ACCENT, C_ACCENT2, C_ACCENT3, C_BG, C_BAD, C_DIM, C_DIM2,
    C_GRAD_A, C_GRAD_B, C_GRAD_B, C_INPUT, C_LINE, C_LINE_HI,
    C_OK, C_RAISED, C_RUN, C_SURFACE, C_TEXT, C_TEXT2, C_WARN,
    STATUS_TEXT, _text_width, font, money, secs, status_color, toks,
)

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
    QTabWidget,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# 各平台该把配置放哪儿，见 tools/userdirs.py

# ⚠️ 端口只在这里写一次。
#
#    原来 8790 在文件里出现了四次（URL、提示文案、两处 _port_open），
#    而 _spawn_backend 起后台时又不传 --port，靠 api_server 自己的默认值。
#    等于**同一个数字写了两份**，改一处就分家 —— 界面找 A、后台听 B。
#
#    现在：只这一个常量，起后台时显式传过去。
_API_PORT = int(os.environ.get("DSH_BRIDGE_PORT") or 8790)
API = f"http://127.0.0.1:{_API_PORT}"

# 启动/扫码这套流程放在 desktop/svc_flow.py 里，
# 因为它和主界面是两套节奏：主界面每 3 秒刷一次任务，
# 而启动服务需要独立跟踪几秒到几十秒，混在一起容易互相拖住。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "desktop"))
try:
    from svc_flow import LoginQRDialog, ServiceMonitor
except Exception:  # noqa: BLE001
    LoginQRDialog = None
    ServiceMonitor = None



class MainWindow(
    TasksPageMixin,
    DepsPageMixin,
    ServicesPageMixin,
    SettingsModelMixin,
    SettingsProfilesMixin,
    SettingsGuardMixin,
    SettingsAuditorMixin,
    SettingsMiscMixin,
    QMainWindow,
):
    """主窗口。

    ⚠️ 继承顺序：mixin 在前，QMainWindow 在最后。

        反过来的话（QMainWindow 在前），MRO 会先找到 Qt 基类的方法，
        mixin 里的同名方法被盖掉 —— 表现是"某些按钮点了没反应"。

    各 mixin 在 desktop/mixins/ 下，按页面分：
        tasks_mixin      任务页
        deps_mixin       环境页
        services_mixin   服务页
        settings_*       设置页的四张卡

    这个类里只留真正"全局"的东西：
        __init__ / _apply_style / 顶栏 / 侧栏 / switch
        _build_settings_page（把各卡拼起来）
        refresh / _on_api / _show_action_error
    """
    def __init__(self):
        super().__init__()
        self.setWindowTitle("dsh-bridge")
        self.resize(1200, 800)
        self.setMinimumSize(940, 640)
        self.tasks: list[dict] = []
        self.filter = "all"
        self._qr_dlg = None
        self._list_limit = 50      # 一次渲染多少行，不够可以再加
        # 模型配置列表
        self._profiles = []
        self._active_pid = ""
        self._selected_pid = ""
        self._profile_error = ""
        self.api = Api()
        self.api.done.connect(self._on_api)

        self._apply_style()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_topbar())
        root.addWidget(GradientBar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_tasks_page())
        self.stack.addWidget(self._build_services_page())
        self.stack.addWidget(self._build_deps_page())
        self.stack.addWidget(self._build_settings_page())
        body.addWidget(self.stack, 1)

        root.addLayout(body, 1)

        self.t = QTimer(self)
        self.t.timeout.connect(self.refresh)
        self.t.start(3000)
        QTimer.singleShot(150, self.refresh)

        # 服务状态用独立节拍器盯着。
        # 它每 1.5 秒问一次，状态一变按钮就跟着变——
        # 这是"启动没反应/一直处理中"那个问题的根本解法。
        if ServiceMonitor is not None:
            self.monitor = ServiceMonitor(self)
            self.monitor.changed.connect(self._on_services)

    # ── 样式 ──
    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QMainWindow, QDialog {{ background:{C_BG}; }}
            QWidget {{
                color:{C_TEXT};
                font-family:"Microsoft YaHei UI","Segoe UI",sans-serif;
            }}

            QFrame#card {{
                background:{C_SURFACE}; border:1px solid {C_LINE};
                border-radius:12px;
            }}
            QFrame#taskrow {{
                background:{C_SURFACE}; border:1px solid {C_LINE};
                border-radius:10px;
            }}
            QFrame#taskrow:hover {{
                background:{C_RAISED}; border-color:{C_LINE_HI};
            }}

            QPushButton#primary {{
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {C_ACCENT}, stop:1 #8b5cf6);
                border:none; color:#ffffff;
                padding:9px 22px; border-radius:8px;
                font-size:13px; font-weight:600;
            }}
            QPushButton#primary:hover {{
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {C_ACCENT2}, stop:1 #7c4ce8);
            }}
            QPushButton#primary:pressed {{ background:{C_ACCENT3}; }}
            QPushButton#primary:disabled {{
                background:{C_RAISED}; color:{C_DIM2};
            }}

            QPushButton#working {{
                background:{C_INPUT}; border:1px solid {C_ACCENT}; color:{C_ACCENT};
                padding:7px 16px; border-radius:8px; font-size:12.5px;
            }}

            QPushButton#ghost {{
                background:transparent; border:1px solid {C_LINE};
                color:{C_TEXT2}; padding:7px 16px; border-radius:8px;
                font-size:12.5px;
            }}
            QPushButton#ghost:hover {{
                background:{C_RAISED}; border-color:{C_LINE_HI}; color:{C_TEXT};
            }}

            QPushButton#nav {{
                background:transparent; border:none; color:{C_DIM};
                text-align:left; padding:10px 16px; border-radius:8px;
                font-size:13px;
            }}
            QPushButton#nav:hover {{ background:{C_SURFACE}; color:{C_TEXT2}; }}
            QPushButton#nav:checked {{
                background:{C_RAISED}; color:{C_TEXT}; font-weight:600;
            }}

            QPushButton#switchoff {{
                background:{C_INPUT}; border:1px solid {C_LINE}; color:{C_DIM};
                padding:7px 18px; border-radius:15px; font-size:12.5px;
            }}
            QPushButton#switchoff:hover {{ border-color:{C_LINE_HI}; color:{C_TEXT2}; }}
            QPushButton#switchon {{
                background:{C_ACCENT}; border:1px solid {C_ACCENT}; color:#fff;
                padding:7px 18px; border-radius:15px; font-size:12.5px;
                font-weight:600;
            }}

            QPushButton#chip {{
                background:transparent; border:1px solid transparent;
                color:{C_DIM}; padding:5px 14px; border-radius:14px;
                font-size:12px;
            }}
            QPushButton#chip:hover {{ color:{C_TEXT2}; background:{C_SURFACE}; }}
            QPushButton#chip:checked {{
                background:{C_RAISED}; border-color:{C_LINE_HI}; color:{C_TEXT};
            }}

            /* ⚠️ 输入框这里踩了一个坑：
               给 QLineEdit 设 setFixedHeight 之后，Qt 会把文字
               按默认的内边距画，结果上半截跑到框外面，
               看起来像"文字被遮挡"——截了三次图才发现不是截断，是位置错了。

               正确做法：**不要 setFixedHeight**，让 Qt 按字体和 padding
               自己算高度；只给一个最小高度兜底。
               这样文字永远在框里，居中。 */
            QLineEdit, QPlainTextEdit, QTextEdit, QComboBox {{
                background:{C_INPUT}; border:1px solid {C_LINE};
                color:{C_TEXT}; border-radius:9px;
                padding:7px 13px;
                font-size:13px; selection-background-color:{C_ACCENT};
            }}
            QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{
                border-color:{C_ACCENT};
            }}
            QComboBox::drop-down {{ border:none; width:22px; }}
            QComboBox QAbstractItemView {{
                background:{C_SURFACE}; border:1px solid {C_LINE_HI};
                border-radius:8px; selection-background-color:{C_ACCENT};
                color:{C_TEXT}; outline:none; padding:4px;
            }}

            QScrollArea {{ border:none; background:transparent; }}
            QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}
            QScrollBar::handle:vertical {{
                background:{C_LINE_HI}; border-radius:3px; min-height:30px;
            }}
            QScrollBar::handle:vertical:hover {{ background:{C_DIM2}; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
            QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}

            QMessageBox, QInputDialog {{ background:{C_SURFACE}; }}
            QMessageBox QLabel, QInputDialog QLabel {{ color:{C_TEXT}; }}
            QMessageBox QPushButton, QInputDialog QPushButton {{
                background:{C_RAISED}; border:1px solid {C_LINE_HI};
                color:{C_TEXT}; padding:6px 18px; border-radius:7px;
                font-size:12.5px; min-width:64px;
            }}
            QMessageBox QPushButton:hover, QInputDialog QPushButton:hover {{
                background:{C_LINE}; 
            }}

            QToolTip {{
                background:{C_RAISED}; color:{C_TEXT};
                border:1px solid {C_LINE_HI}; padding:7px 10px;
                border-radius:6px; font-size:12px;
            }}
        """)

    # ── 顶栏 ──
    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setFixedHeight(54)
        bar.setStyleSheet(f"background:{C_SURFACE}; border-bottom:1px solid {C_LINE};")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(22, 0, 22, 0)
        lay.setSpacing(12)

        mark = QLabel("dsh-bridge")
        mark.setFont(font(14, QFont.Bold))
        mark.setStyleSheet(f"color:{C_TEXT};")
        lay.addWidget(mark)
        lay.addStretch()

        # 顶栏三个状态胶囊。
        #
        # ⚠️ 第一版这里出了问题：QFrame 默认会被父布局拉满高度，
        #    胶囊变成三个高瘦的长条，中间还露出奇怪的竖线。
        #    必须显式 setFixedHeight，并且用 AlignVCenter 传到布局里。
        self.pills = {}
        for key, label in (("qq", "QQ"), ("feishu", "飞书"), ("bridge", "任务端")):
            pill = QFrame()
            pill.setFixedHeight(26)
            pill.setStyleSheet(
                f"QFrame {{ background:{C_RAISED}; border:1px solid {C_LINE};"
                f"border-radius:13px; }}"
            )
            pl = QHBoxLayout(pill)
            pl.setContentsMargins(10, 0, 12, 0)
            pl.setSpacing(6)
            dot = GlowDot(C_DIM2, 6)
            lb = QLabel(label)
            lb.setFont(font(11.5))
            lb.setStyleSheet(f"color:{C_DIM}; background:transparent; border:none;")
            pl.addWidget(dot)
            pl.addWidget(lb)
            pill.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.pills[key] = dot
            lay.addWidget(pill, 0, Qt.AlignVCenter)

        return bar

    # ── 侧栏 ──
    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setFixedWidth(184)
        side.setStyleSheet(f"background:{C_SURFACE}; border-right:1px solid {C_LINE};")
        lay = QVBoxLayout(side)
        lay.setContentsMargins(12, 16, 12, 16)
        lay.setSpacing(4)

        self.nav_btns = []
        for text, idx in (("任务", 0), ("服务", 1), ("环境", 2), ("设置", 3)):
            b = QPushButton(text)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, i=idx: self.switch(i))
            lay.addWidget(b)
            self.nav_btns.append(b)
        self.nav_btns[0].setChecked(True)

        lay.addStretch()
        # ⚠️ 版本号读 VERSION 文件，别写死。
        #
        #    原来这里是 QLabel("v1.3.2") —— 每次发版都要记得改这里，
        #    忘了界面上就一直显示旧版本号，用户会以为没更新。
        #
        #    现在从仓库根的 VERSION 读。读不到就显示 "dev"，
        #    至少不会显示一个错的版本。
        ver = QLabel(_app_version())
        ver.setFont(font(10))
        ver.setStyleSheet(f"color:{C_DIM2}; padding:0 16px;")
        lay.addWidget(ver)
        return side

    def switch(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)
        for i, b in enumerate(self.nav_btns):
            b.setChecked(i == idx)
        if idx == 2:
            self._refresh_deps()
        if idx == 3:
            self._load_security_mode()
            self._load_profiles()
            # 审核员配置也拉一下（同样很便宜）
            self._load_auditor()
            self._load_paths()
            self._load_guard()
            self._refresh_autostart()

    # ── 任务页 ──
    def _build_settings_page(self) -> QWidget:
        # 设置页内容长（三张卡 + 五条路径），小窗口下会超出高度。
        # 不包滚动区的话，Qt 会优先把控件压扁来腾地方——
        # 实测出现过输入框被压成一条细线、标签糊在一起。
        #
        # 所以结构是：
        #     page(滚动区) -> inner(真正的内容) -> 各种卡片
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setStyleSheet("QScrollArea { border:none; background:transparent; }")

        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(16)

        h = QLabel("设置")
        h.setFont(font(19, QFont.Bold))
        h.setStyleSheet(f"color:{C_TEXT};")
        sub = QLabel("改了立刻生效，不用重启。")
        sub.setFont(font(12))
        sub.setStyleSheet(f"color:{C_DIM};")
        lay.addWidget(h)
        lay.addWidget(sub)


        # 四张卡各自独立成方法——改哪张直接跳过去，
        # 不用在这个方法里翻几百行。
        #
        # ⚠️ 安全模式在最前面：它决定了下面那些卡还生效不生效。
        self._card_security_mode(lay)
        self._card_model(lay)
        self._card_guard(lay)
        self._card_autostart(lay)
        self._card_auditor(lay)
        self._card_paths(lay)

        # 收尾：把内容塞进滚动区、把滚动区返回给调用方。
        # （这两行原来在方法最末尾；拆卡片时被切进了 _card_paths，
        #   那里没有 page/inner 变量，启动就 NameError。）
        page.setWidget(inner)
        return page

    def refresh(self) -> None:
        self.api.get("/api/tasks")
        self.api.get("/api/services")
        # 模型配置也每轮拉一次——读的是本地 json，很便宜。
        # 用户新加一套模型不用重启界面，任务页的分组就跟着变了。
        self._load_profiles()

    def _on_api(self, path: str, data) -> None:
        if path == "/api/security/mode" and isinstance(data, dict):
            self._on_security_mode(data)
        elif path == "/api/auditor" and isinstance(data, dict):
            self._on_auditor(data)
        elif path == "/api/tasks" and isinstance(data, list):
            self.tasks = data
            self._render_tasks()
        elif path == "/api/services" and isinstance(data, dict):
            for k, dot in self.pills.items():
                dot.set_color(C_OK if data.get(k) else C_BAD)
            for k, card in self.svc_cards.items():
                card.set_ok(bool(data.get(k)))
        elif path == "/api/model" and isinstance(data, dict):
            if data.get("preset"):
                i = self.cmb_model.findData(data["preset"])
                if i >= 0:
                    self.cmb_model.setCurrentIndex(i)
            self.lbl_now.setText(
                f"现在用的是：{data.get('provider_now')} / {data.get('model_now')}"
            )
        elif path in ("/api/dispatch", "/api/continue", "/api/cancel"):
            # ⚠️ 这里原来只有一句 self.refresh()。
            #
            #    后端拒绝派活时会回 {"error": ..., "hint": ..., "code": ...}，
            #    而这里什么都不看，直接刷新列表 ——
            #    新手看到的就是「点了按钮，什么都没发生」。
            #
            #    实测：全新环境点派活 → 后端说没有可用模型 →
            #    前端无反应 → 用户完全不知道该怎么办。
            self._show_action_error(path, data)
            self.refresh()

    def _show_action_error(self, path: str, data) -> None:
        """把「派活没成」的原因告诉用户。

        ⚠️ 为什么会需要这个方法：

            原来前端对 /api/dispatch 的返回**什么都不看**，只 refresh。
            而后端拒绝时回的是 error + hint，
            新手看到的就是「点了按钮，什么都没发生」。

            而他真正缺的可能是：没加模型、名字重了、被闸门拦了……
            每种情况的下一步都不一样，必须说出来。
        """
        if not isinstance(data, dict):
            return

        err = str(data.get("error") or "").strip()
        if not err:
            return

        hint = str(data.get("hint") or "").strip()
        code = str(data.get("code") or "").strip()

        # 没有模型：给一个「去设置」按钮，别让用户自己找
        if code == "no_model":
            box = QMessageBox(self)
            box.setWindowTitle("还差一步")
            box.setIcon(QMessageBox.Information)
            box.setText(err)
            if hint:
                box.setInformativeText(hint)
            b_go = box.addButton("去设置", QMessageBox.AcceptRole)
            box.addButton("知道了", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is b_go:
                # 任务 / 服务 / 环境 / 设置 —— 设置是第 3 个（从 0 数）
                self.stack.setCurrentIndex(3)
            return

        # 其他情况：原因和提示都摆出来
        msg = err if not hint else f"{err}\n\n{hint}"
        QMessageBox.warning(self, "没成", msg)

def _port_open(port: int, timeout: float = 0.5) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _make_kill_on_close_job():
    """建一个「关闭即杀」的 Job。失败返回 None（不致命）。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = EXT()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            k32.CloseHandle(job)
            return None
        return job
    except Exception:  # noqa: BLE001
        return None


def _assign_to_job(job, pid: int) -> bool:
    try:
        import ctypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1F0FFF, False, pid)
        if not h:
            return False
        try:
            return bool(k32.AssignProcessToJobObject(job, h))
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return False


def _spawn_backend() -> bool:
    """把接口服务拉起来。已有一个就不动它。成功返回 True。"""
    global _BACKEND, _BACKEND_JOB
    import subprocess
    import time

    if _port_open(_API_PORT):
        # 已经有人听 —— 复用，不杀不抢
        return True

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    api_py = os.path.join(here, "server", "api_server.py")
    if not os.path.exists(api_py):
        return False

    # 用 pythonw 起，别开黑框
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable

    # ⚠️ 必须显式传端口。
    #
    #    不传的话它用自己的 DEFAULT_PORT，而这个文件里的端口是另一个常量 ——
    #    两个数一旦不一致，界面就在找一个没人听的端口。
    #    端口冲突时也一样：后台起不来，界面永远转圈。
    flags = 0x08000000  # CREATE_NO_WINDOW
    try:
        _BACKEND = subprocess.Popen(
            [pyw, api_py, "--port", str(_API_PORT)],
            cwd=here,
            # 输出别丢：起不来的时候要看它说了什么
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
        )
    except OSError as _e:
        print(f"[ui] 起后台失败：{_e}", file=sys.stderr)
        return False

    # 挂进 Job：界面退出 → 系统自动杀掉它
    _BACKEND_JOB = _make_kill_on_close_job()
    if _BACKEND_JOB and not _assign_to_job(_BACKEND_JOB, _BACKEND.pid):
        # 挂不上不是致命问题，但记一笔
        print("[ui] 后台没能挂进 Job，关窗口后它可能还在跑", file=sys.stderr)

    # 等它能响应
    for _ in range(60):
        time.sleep(0.4)
        if _port_open(_API_PORT):
            return True
        if _BACKEND.poll() is not None:
            break

    # 没等到 —— 把后台说的话捞回来，别静默。
    # 用户看到「界面是空的」却不知道为什么，比看到一段报错还难受。
    _out = ""
    try:
        if _BACKEND.stdout is not None:
            _out = _BACKEND.stdout.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        pass
    if _out.strip():
        print("[ui] 后台启动输出：", file=sys.stderr)
        print(_out[:2000], file=sys.stderr)
    return False
    return False


def _app_version() -> str:
    """从仓库根的 VERSION 读版本号。

    ⚠️ 原来版本号是写死在界面代码里的（QLabel("v1.3.2")）。
       每次发版都得记得改那一行，忘了用户就看到旧版本号。

    现在只维护 VERSION 一个文件。
    读不到就返回 "dev" —— 至少不会显示一个可能是错的版本。
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(here, "VERSION"), encoding="utf-8") as f:
            v = f.read().strip()
        return f"v{v}" if v else "dev"
    except OSError:
        return "dev"


def main() -> int:
    # 先保证后台在跑。失败也继续开界面 ——
    # 界面自己会提示「连不上后台」，比直接打不开好。
    if not _spawn_backend():
        print("[ui] 后台没起来，界面会是空的", file=sys.stderr)

    app = QApplication(sys.argv)
    app.setApplicationName("dsh-bridge")
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
