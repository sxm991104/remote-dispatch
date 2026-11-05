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

import json
import os
import sys
import threading

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
    C_ACCENT, C_ACCENT2, C_ACCENT3, C_ACCENT_TXT, C_BG, C_BAD, C_DIM, C_DIM2,
    C_GRAD_A, C_GRAD_B, C_INPUT, C_LINE, C_LINE_HI,
    C_OK, C_RAISED, C_RUN, C_SURFACE, C_TEXT, C_TEXT2, C_WARN,
    STATUS_TEXT, _text_width, font, money, secs, status_color, toks,
)

from PySide6.QtCore import QLockFile, QObject, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
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
import tools.userdirs as _userdirs

# 导航图标目录（和设计稿同一套 stroke 图标）
_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

# 七页的标题/副标题（顶栏照设计稿显示当前页的标题）
PAGE_META = [
    ("概览", "一眼看清现在能不能用。"),
    ("任务", "派出去让电脑干的活，都在这儿。"),
    ("通道", "消息从哪个聊天软件进来 —— atrium 管这一层。"),
    ("模型", "任务端用哪套模型，随时切换。"),
    ("安全", "验证码、审核员、防护档位。"),
    ("权限", "每个任务窗口能动多大地方，联网要不要放行。"),
    ("诊断", "看现场：哪块通了、哪块卡了、怎么修。"),
    ("系统", "依赖、开机自启、版本。"),
]

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



class _BackendBoot(QObject):
    """在后台线程里把接口服务拉起来。

    ⚠️ 不能在主线程里等端口 —— 那等于界面卡住。
        这里只负责"等/起"，结果用信号交回主线程，
        界面那条提示条再根据 True/False 收尾。
    """

    finished = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._th: threading.Thread | None = None

    def is_running(self) -> bool:
        return self._th is not None and self._th.is_alive()

    def start(self) -> None:
        if self.is_running():
            return
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def _run(self) -> None:
        try:
            ok = _spawn_backend()
        except Exception:  # noqa: BLE001
            ok = False
        self.finished.emit(ok)


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

        # 后台线程要回主线程做的事，先排到这个队列里，
        # 由主线程的定时器统一执行（见 _drain_main_queue）。
        #
        # ⚠️ 为什么不用 Signal：
        #    Signal 只能在**类定义**里声明，不能在 __init__ 里动态建
        #    （`Signal(object)()` 那个写法得到的不是能 connect 的信号）。
        #
        # ⚠️ 为什么不用 QMetaObject.invokeMethod：
        #    它只认成员方法名字符串，不接受 Python 函数/Lambda，
        #    传进去直接 TypeError。
        #    （2026-11-05 点「检查更新」就崩在这儿。）
        #
        # 这个队列 + 定时器的做法最简单可靠：后台线程只往 list 里塞，
        # 主线程每 50ms 取一次执行。
        self._main_queue: list = []
        self._main_timer = QTimer(self)
        self._main_timer.timeout.connect(self._drain_main_queue)
        self._main_timer.start(50)

        # 窗口大小/位置记在配置目录里，下次开还在这儿。
        # 用 ini 文件而不是注册表：跟项目其它配置放一起，看得见、能删。
        self._ui = QSettings(
            _userdirs.config_file("desktop.ini"), QSettings.IniFormat
        )

        # ⚠️ 页面按需构建。
        #
        #   原来 __init__ 里把任务/服务/环境/设置四页全画出来，
        #   而用户启动后大多数时间只看任务页 —— 设置页有几十个
        #   输入框、路径行、开关，画完就放着，白白拖慢首屏。
        #
        #   现在先只建概览页（第一屏就要看），其余页等第一次切过去再建。
        #
        #   七个模块（按用户能配的东西分，不是按技术分）：
        #       0 概览   一眼看清现在能不能用
        #       1 任务   派出去的活
        #       2 通道   atrium：飞书/钉钉进来的门
        #       3 模型   任务端多套模型
        #       4 安全   安全模式 / 验证码邮箱 / 审核员
        #       5 权限   窗口权限档 / 联网授权
        #       6 系统   依赖 / 开机自启 / 版本
        self._pages: dict[int, QWidget] = {}
        self._page_builders = {
            0: self._build_overview_page,
            1: self._build_tasks_page,
            2: self._build_channels_page,
            3: self._build_models_page,
            4: self._build_security_page,
            5: self._build_perms_page,
            6: self._build_diagnose_page,
            7: self._build_system_page,
        }
        self._backend_ready = False

        self._apply_style()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_topbar())
        root.addWidget(GradientBar())
        # 后台没起来时挂一条提示，不挡界面，也不让用户对着空列表猜。
        root.addWidget(self._build_boot_bar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        # 先占位，切到哪页才真建哪页。
        #
        # ⚠️ 占位数量**必须跟 _page_builders 一样多**。
        #
        #    原来写死 range(7)，而 builder 有 8 个（0..7）——
        #    第 8 页（系统页）压根没有占位控件。
        #
        #    后果不止是"系统页空白"：
        #        _ensure_page 是 insertWidget(idx, page) 再删占位，
        #        索引一旦对不上，后面所有页都会错位 ——
        #        安全页拿到的可能是权限页的占位，数据请求发错地方。
        #
        #    实测（2026-11-05）：
        #        · 安全模式三个选项一个都不亮（数据没回来）
        #        · 系统页的 switch_auto / lbl_auto 根本不存在
        #        · 点开机自启，回调里 AttributeError，界面永不更新
        #
        #    用 len(self._page_builders) 而不是写死数字：
        #    以后加页不用回来改这一行。
        # ⚠️ 占位控件也要给父控件（就是 stack 本身）。
        #
        #    QWidget() 不带父控件时尺寸是 Qt 默认的 640x480，
        #    虽然紧接着 addWidget 会收养它，但在那一瞬间
        #    系统层面能看到一个 640x480 的顶层窗口闪一下。
        #
        #    实测（2026-11-05）：每切到一个新页面，都能查到一个
        #    新的 QWidget 640x480 顶层窗口。
        #
        #    传 self.stack 当父控件就没有这一瞬间了。
        for _ in range(len(self._page_builders)):
            self.stack.addWidget(QWidget(self.stack))
        body.addWidget(self.stack, 1)

        root.addLayout(body, 1)
        self._ensure_page(0)
        # insert/remove 占位控件后，stack 当前页可能被重置，
        # 显式钉回第 0 页，否则首开内容区是一片黑、要点一下导航才出。
        self.stack.setCurrentIndex(0)

        self.t = QTimer(self)
        self.t.timeout.connect(self.refresh)
        self.t.start(3000)

        # 服务状态用独立节拍器盯着。
        # 它每 1.5 秒问一次，状态一变按钮就跟着变——
        # 这是"启动没反应/一直处理中"那个问题的根本解法。
        #
        # ⚠️ 后台还没起来时先暂停：那几秒里每问一次都是失败，
        #    除了耗线程没有意义。起来了再让它开始问。
        if ServiceMonitor is not None:
            self.monitor = ServiceMonitor(self)
            self.monitor.changed.connect(self._on_services)
            self.monitor.pause()

        # 窗口先画出来，再去等/拉后台。见 _boot_backend。
        self._boot = None
        QTimer.singleShot(0, self._boot_backend)
        self._restore_geometry()

    # ── 页面按需构建 ──
    def _ensure_page(self, idx: int) -> None:
        """这一页还没建就先建出来，占位控件换掉。

        ## ⚠️ 构建期间要**禁止窗口显示**

            老爸的原话：「重启后第一次点模型会闪几个小窗口，
            后面再点就没了。」

            原因就在这个函数：页面是懒建的，第一次点才建。
            建的过程中会 new 出几十个控件，其中不少是
            `QFrame()` / `QWidget()` 这种**不带父控件**的写法 ——
            Qt 会把它们先当成**独立顶层窗口**，系统层面真的会画一帧。

            实测（2026-11-05）：
                用 EnumWindows 每 15ms 扫机器，抓到的三个闪窗：
                    571x85  存在 18ms
                    508x52  存在 18ms
                    540x85  存在 16ms
                    类名全是 Qt6120QWindowIcon，pid 就是桌面端。
                查 Qt 的 topLevelWidgets，见到的孤儿：
                    QScrollArea 640x480
                    QWidget     640x480
                    QFrame      100x30（权限页一次 55 个）

            **与其挨个给几百个控件补父窗口（漏一个就还闪），
              不如在这一层统一处理：**

                建页面之前  → setUpdatesEnabled(False) + 记下当前可见性
                建页面之后  → 恢复可见性 + setUpdatesEnabled(True)

            setUpdatesEnabled(False) 期间，新控件不会被排进绘制队列，
            屏幕不会画；建完再一次性画出来。

            这样以后谁再写 `QFrame()` 都不会再闪了 ——
            不用靠"记得给父控件"这种自觉。
        """
        if idx in self._pages:
            return
        builder = self._page_builders.get(idx)
        if builder is None:
            return

        was_updating = self.updatesEnabled()
        self.setUpdatesEnabled(False)
        try:
            page = builder()

            ph = self.stack.widget(idx)
            # 插到占位控件前面，再把占位删掉 —— 这样索引不会乱
            self.stack.insertWidget(idx, page)
            if ph is not None:
                self.stack.removeWidget(ph)
                ph.deleteLater()
            self._pages[idx] = page
        finally:
            self.setUpdatesEnabled(was_updating)

    # ── 窗口位置 ──
    def _restore_geometry(self) -> None:
        geo = self._ui.value("window/geometry")
        if geo is not None:
            try:
                self.restoreGeometry(geo)
            except Exception:  # noqa: BLE001
                pass

        # 上次停在哪个页面，等后台起来了再切过去
        # （后台没好就切，设置页会先闪一屏"读不到配置"）
        try:
            last = int(self._ui.value("window/page", 0) or 0)
        except (TypeError, ValueError):
            last = 0
        self._restore_page_idx = last if last in self._page_builders else 0

    def closeEvent(self, e):  # noqa: N802
        try:
            self._ui.setValue("window/geometry", self.saveGeometry())
            self._ui.setValue("window/page", self.stack.currentIndex())
            self._ui.sync()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(e)

    def hideEvent(self, e):  # noqa: N802
        # 最小化/切走时不轮询：界面看不见，刷了也没人看，
        # 但每 3 秒三个请求 + 每 1.5 秒一个请求是实打实的开销。
        if getattr(self, "t", None) is not None:
            self.t.stop()
        if getattr(self, "monitor", None) is not None:
            self.monitor.pause()
        super().hideEvent(e)

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        if getattr(self, "t", None) is not None and not self.t.isActive():
            self.t.start(3000)
        if getattr(self, "monitor", None) is not None and self._backend_ready:
            self.monitor.resume()
        # 回来时立刻补一次，别让用户先看到一屏旧数据
        if self._backend_ready:
            self.refresh()

    # ── 后台启动 ──
    def _build_boot_bar(self) -> QWidget:
        """后台还在起的时候挂在这一条上。

        为什么不弹对话框：用户只是打开程序，没有做错什么，
        不该被一个模态窗拦住；等几秒自己消失就行。
        """
        bar = QFrame()
        bar.setObjectName("bootbar")
        bar.setFixedHeight(36)
        bar.setStyleSheet(
            f"QFrame#bootbar {{ background:{C_INPUT};"
            f" border-bottom:1px solid {C_LINE}; }}"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(22, 0, 22, 0)
        lay.setSpacing(8)

        self._boot_spin = BusyIndicator(14)
        lay.addWidget(self._boot_spin, 0, Qt.AlignVCenter)

        self._boot_lbl = QLabel("正在启动后台服务…")
        self._boot_lbl.setFont(font(11.5))
        self._boot_lbl.setStyleSheet(f"color:{C_DIM};")
        lay.addWidget(self._boot_lbl, 0, Qt.AlignVCenter)
        lay.addStretch()

        self._boot_retry = QPushButton("重试")
        self._boot_retry.setObjectName("ghost")
        self._boot_retry.setFixedHeight(24)
        self._boot_retry.setCursor(Qt.PointingHandCursor)
        self._boot_retry.clicked.connect(self._boot_backend)
        self._boot_retry.hide()
        lay.addWidget(self._boot_retry, 0, Qt.AlignVCenter)

        self._boot_bar = bar
        return bar

    def _boot_backend(self) -> None:
        """把等后台这件事挪到后台线程。

        ⚠️ 原来 _spawn_backend 是在 QApplication 之前调的：
            冷启动时它要起进程、等端口，最长 24 秒，
            这段时间里**窗口根本没画出来**——
            用户双击图标，几秒内什么都没发生，很容易以为没点着。

            现在窗口先出来，后台在后台线程里起，
            进度显示在上面那条，起好了自己消失。
        """
        if getattr(self, "_boot", None) is not None and self._boot.is_running():
            return
        self._boot_retry.hide()
        self._boot_spin.show()
        self._boot_lbl.setText("正在启动后台服务…")
        self._boot_lbl.setStyleSheet(f"color:{C_DIM};")
        self._boot_bar.show()

        self._boot = _BackendBoot(self)
        self._boot.finished.connect(self._on_backend)
        self._boot.start()

    def _on_backend(self, ok: bool) -> None:
        self._backend_ready = bool(ok)

        if ok:
            self._boot_bar.hide()
            if getattr(self, "monitor", None) is not None:
                self.monitor.resume()
            self.refresh()
            last = getattr(self, "_restore_page_idx", 0)
            if last:
                self._restore_page_idx = 0
                self.switch(last)
            return

        # 没起来：把话说清楚，给一个重试；界面照常能用（只是没数据）
        self._boot_spin.hide()
        self._boot_lbl.setText("后台服务没起来，界面上的数据可能不全。")
        self._boot_lbl.setStyleSheet(f"color:{C_BAD};")
        self._boot_retry.show()

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
                background:{C_ACCENT};
                border:none; color:{C_ACCENT_TXT};
                padding:9px 22px; border-radius:8px;
                font-size:13px; font-weight:600;
            }}
            QPushButton#primary:hover {{
                background:{C_ACCENT2};
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
                background:rgba(224,168,60,0.14); color:{C_ACCENT}; font-weight:600;
                border:1px solid rgba(224,168,60,0.25);
            }}

            QPushButton#switchoff {{
                background:{C_INPUT}; border:1px solid {C_LINE}; color:{C_DIM};
                padding:7px 18px; border-radius:15px; font-size:12.5px;
            }}
            QPushButton#switchoff:hover {{ border-color:{C_LINE_HI}; color:{C_TEXT2}; }}
            QPushButton#switchon {{
                background:{C_ACCENT}; border:1px solid {C_ACCENT}; color:{C_ACCENT_TXT};
                padding:7px 18px; border-radius:15px; font-size:12.5px;
                font-weight:600;
            }}

            QPushButton#chip {{
                background:transparent; border:1px solid transparent;
                color:{C_DIM}; padding:5px 14px; border-radius:14px;
                font-size:12px;
            }}
            QPushButton#chip:hover {{ color:{C_TEXT2}; background:{C_SURFACE}; }}
            /* ⚠️ chip:checked 原来是灰的（C_RAISED + C_LINE_HI），
               选没选中几乎看不出区别 —— 用户以为"点了没反应"。

               改成主强调色（暖琥珀），跟左边导航选中项、主按钮一个色。
               同一种"这里被选中了"的语义，全站一个颜色。 */
            QPushButton#chip:checked {{
                background:{C_ACCENT}; border-color:{C_ACCENT};
                color:{C_ACCENT_TXT}; font-weight:600;
            }}
            QPushButton#chip:checked:hover {{
                background:{C_ACCENT2}; border-color:{C_ACCENT2};
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

        # 左：当前页标题 + 副标题（照设计稿，顶栏即页面标题）
        title_col = QVBoxLayout(); title_col.setSpacing(1)
        title_col.setContentsMargins(0, 0, 0, 0)
        self._top_title = QLabel(PAGE_META[0][0])
        self._top_title.setFont(font(15, QFont.Bold))
        self._top_title.setStyleSheet("background:transparent;")
        self._top_sub = QLabel(PAGE_META[0][1])
        self._top_sub.setFont(font(11))
        self._top_sub.setStyleSheet(f"color:{C_DIM}; background:transparent;")
        title_col.addWidget(self._top_title)
        title_col.addWidget(self._top_sub)
        lay.addLayout(title_col)
        lay.addStretch()

        # 顶栏状态胶囊。
        #
        # ⚠️ 第一版这里出了问题：QFrame 默认会被父布局拉满高度，
        #    胶囊变成三个高瘦的长条，中间还露出奇怪的竖线。
        #    必须显式 setFixedHeight，并且用 AlignVCenter 传到布局里。
        #
        # ⚠️ 顶栏**不挂 QQ 那个灯**。
        #
        #    派活台的通道是飞书和钉钉（走 atrium）。
        #    QQ 个人号那条路（QQ 客户端 + NapCat）不在这条线上，
        #    挂个灯只会永远灭着，用户以为坏了。
        #
        #    （AstrBot / NapCat 的对接代码在仓库里留着，
        #      但那是另一条路，不在派活台的通道里。）
        #
        # ⚠️ 现在加上「钉钉」。
        #    老爸截图里就问过："配置了几个几个绿吗"。
        #    原来只挂飞书和任务端，钉钉明明配好了却看不见。
        #
        # 这三个灯的颜色来自 /api/services，那个接口现在会真去
        # 读 atrium 的心跳和配置（以前三个值全是写死的常量）。
        self.pills = {}
        for key, label in (
            ("feishu", "飞书"),
            ("dingtalk", "钉钉"),
            ("bridge", "任务端"),
        ):
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
        side.setFixedWidth(208)
        side.setStyleSheet(f"background:{C_SURFACE}; border-right:1px solid {C_LINE};")
        lay = QVBoxLayout(side)
        lay.setContentsMargins(12, 16, 12, 16)
        lay.setSpacing(3)

        # 顶部 logo 块
        logo = QHBoxLayout(); logo.setSpacing(10)
        mark = QLabel("派")
        mark.setFixedSize(30, 30)
        mark.setAlignment(Qt.AlignCenter)
        mark.setStyleSheet(
            f"background:{C_ACCENT}; color:{C_ACCENT_TXT}; border-radius:8px;"
            f"font-weight:700; font-size:14px;")
        lt = QVBoxLayout(); lt.setSpacing(0)
        lt.addWidget(QLabel("派活台"))
        sub = QLabel(_app_version())
        sub.setFont(font(10)); sub.setStyleSheet(f"color:{C_DIM2};")
        lt.addWidget(sub)
        logo.addWidget(mark); logo.addLayout(lt)
        lay.addLayout(logo)
        lay.addSpacing(14)

        self.nav_btns = []

        def _group(label: str):
            lb = QLabel(label)
            lb.setFont(font(10))
            lb.setStyleSheet(f"color:{C_DIM2}; padding:12px 10px 4px;")
            lay.addWidget(lb)

        def _item(text: str, idx: int, icon: str):
            b = QPushButton(text)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setIcon(QIcon(os.path.join(_ICON_DIR, icon + ".svg")))
            b.setIconSize(QSize(17, 17))
            b._icon_name = icon
            b.clicked.connect(lambda _=False, i=idx: self.switch(i))
            lay.addWidget(b)
            self.nav_btns.append(b)

        _group("干活")
        _item("概览", 0, "overview")
        _item("任务", 1, "tasks")
        _group("配置")
        _item("通道", 2, "channels")
        _item("模型", 3, "models")
        _item("安全", 4, "security")
        _item("权限", 5, "perms")
        _item("诊断", 6, "diagnose")
        _item("系统", 7, "system")

        self.nav_btns[0].setChecked(True)

        lay.addStretch()

        # 底部任务端状态卡
        #
        # ⚠️ 这里原来写的是「后台服务」，这个词会误导：
        #
        #    咱们这套里「前台」指的是聊天里跟你说话的那个（atrium 的 Agent），
        #    而这个框里的东西是 **127.0.0.1:8790 那个服务** ——
        #    真正在电脑上干活的那一头。
        #
        #    顶栏那个灯写的「任务端」才是对的。这里统一。
        #
        # ⚠️ 而且它原来**永远显示「正在连接…」**：
        #    创建时赋一次初值，全项目再没有人更新它。
        #    桥服务明明活着，顶栏灯是绿的，左下角却一直说在连。
        #    （2026-11-05 真开窗口看到的。）
        eng = QFrame(); eng.setObjectName("card")
        el = QVBoxLayout(eng); el.setContentsMargins(12, 10, 12, 10); el.setSpacing(2)
        et = QLabel("任务端"); et.setFont(font(11, QFont.DemiBold)); el.addWidget(et)
        self._side_engine = QLabel("正在连…")
        self._side_engine.setFont(font(10))
        self._side_engine.setStyleSheet(f"color:{C_DIM2};")
        el.addWidget(self._side_engine)
        lay.addWidget(eng)
        return side

    def switch(self, idx: int) -> None:
        self._ensure_page(idx)
        self.stack.setCurrentIndex(idx)
        # 顶栏标题跟着当前页走
        t, s = PAGE_META[idx]
        self._top_title.setText(t)
        self._top_sub.setText(s)
        for i, b in enumerate(self.nav_btns):
            on = i == idx
            b.setChecked(on)
            # 选中项图标换成琥珀版，其余用灰色版
            nm = getattr(b, "_icon_name", None)
            if nm:
                b.setIcon(QIcon(os.path.join(
                    _ICON_DIR, nm + ("-on.svg" if on else ".svg"))))
        # 切到哪页就拉哪页的数据，别一次性全拉。
        #
        # ⚠️ 这里的分派必须跟 _page_builders 的索引一一对上。
        #
        #    实际的页面顺序：
        #        0 概览  1 任务  2 通道  3 模型
        #        4 安全  5 权限  6 诊断  7 系统
        #
        #    原来把"系统"当成 idx==6，而 6 其实是诊断页 ——
        #    于是开机自启的数据请求发到了诊断页，
        #    系统页那三个控件（switch_auto / lbl_auto）压根没人管：
        #        · _auto_current 不存在 → 点开关 AttributeError
        #        · 界面永远显示旧状态
        #
        #    实测（2026-11-05）：老爸点开机自启，磁盘都改了，界面不动。
        if idx == 1:
            self._render_tasks()
        elif idx == 3:
            self._load_profiles()
        elif idx == 2:
            self._load_atrium()
        elif idx == 4:
            self._load_security_mode()
            self._load_auditor()
            self._load_guard()
        elif idx == 5:
            self._load_perms()
            self._load_net_status()
        elif idx == 6:
            # 诊断页：跑一遍自检。
            #
            # ⚠️ 原来这里调的是 _refresh_deps()（依赖列表），
            #    而 deps_box 是**系统页**建的。诊断页没那个控件 →
            #        AttributeError: 'MainWindow' object has no attribute 'deps_box'
            #    切到诊断页就崩，而且异常被 Qt 吞了，界面看着就是"点不动"。
            #
            #    实测（2026-11-05）：把每个页面的按钮挨个点了一遍才发现。
            self._load_diagnose()
        elif idx == 7:
            # 系统页：依赖列表 + 开机自启 + 版本
            self._refresh_deps()
            self._refresh_autostart()

    # ── 七个页面 ──
    def _drain_main_queue(self) -> None:
        """主线程每 50ms 跑一遍：把后台线程排进来的活干完。

        ⚠️ 后台线程碰界面是崩的（Qt 只允许主线程动控件）。
            所以后台只管往 self._main_queue 里塞函数，
            主线程在这里执行。

            测一遍就取空 —— 没活的时候几乎不花时间。
        """
        if not getattr(self, "_main_queue", None):
            return
        # 先把这一轮要跑的取出来，避免执行中又塞进来的被 run 掉导致死循环
        todo = self._main_queue[:]
        self._main_queue.clear()
        for fn in todo:
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _scroll_page() -> tuple[QScrollArea, QVBoxLayout]:
        """做一个内容可滚动的页，返回 (滚动区, 里面的竖布局)。

        ⚠️ **QScrollArea() 创建时没父控件，它自己就是个顶层窗口。**

            实测（2026-11-05，老爸说"重启后第一次点模型会闪几个小窗口，
            后面再点就没了"）：

                用 Qt 的 topLevelWidgets() 在模型页建好后查：
                    QScrollArea  640x480  可见=False
                    QFrame       100x30   可见=False

                再用 Win32 EnumWindows 每 15ms 扫一遍系统：
                    571x85 存在 18ms
                    508x52 存在 18ms
                    540x85 存在 16ms
                    类名全是 Qt6120QWindowIcon，pid 就是桌面端。

            也就是说：虽然 visible=False，但 Qt 建顶层窗口那一帧
            系统会真画一次，屏幕上闪出白块。

            **而且全站都有这个问题** —— 每个页面都走这个函数，
            每个页面第一次打开都会建一个新 QScrollArea。
            只是"第一次才有"容易被误以为是偶发。

        ⚠️ 修法：**先挂到一个藏起来的临时父控件上。**

            不能简单 setVisible(False)：Qt 里显式隐藏过的控件，
            父容器 show 的时候它也不会显示（整页空白）。

            用 holder 当爹，page 一出生就不是顶层窗口，
            建完再 setParent(None) 摘出来，交给调用方 addWidget。
        """
        holder = QWidget()
        holder.hide()

        page = QScrollArea(holder)
        page.setWidgetResizable(True)
        page.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        inner = QWidget()
        # ⚠️ 必须显式给底色：不然空白页是 Windows 默认的白色，
        #    深卡片浮在白底上，看着像没换主题。
        #
        # ⚠️ 必须用带 objectName 的作用域选择器，不能写裸 `background:`。
        #    裸声明会被当成子树通用规则，把里面 QPushButton#primary 的
        #    琥珀底也盖成背景色（实测按钮变成 C_BG）。
        inner.setObjectName("pagebg")
        inner.setStyleSheet(f"QWidget#pagebg {{ background:{C_BG}; }}")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(16)
        page.setWidget(inner)

        # 从临时父控件里摘出来，交给调用方
        page.setParent(None)
        holder.deleteLater()

        return page, lay

    def _build_overview_page(self) -> QWidget:
        """概览：一眼看清现在能不能用。"""
        page, lay = self._scroll_page()

        # 三个大数字
        #
        # 第三个原来叫「今天花掉」，太口语，而且看不出是"全部模型加起来"。
        # 改成「今日总花销」—— 所有配置的模型今天花掉的钱合起来。
        row = QHBoxLayout()
        row.setSpacing(12)
        self._ov_running = QLabel("0")
        self._ov_done = QLabel("0")
        self._ov_cost = QLabel("¥0.00")
        for value_lbl, title, color in (
            (self._ov_running, "正在跑的任务", C_TEXT),
            (self._ov_done, "今天完成", C_OK),
            (self._ov_cost, "今日总花销", C_TEXT),
        ):
            card = QFrame(); card.setObjectName("card")
            # 设计稿里统计卡是矮条（label 在上、大数字在下），不能被竖向拉成大高个。
            card.setFixedHeight(112)
            cl = QVBoxLayout(card); cl.setContentsMargins(18, 14, 18, 14)
            tit = QLabel(title); tit.setFont(font(10.5)); tit.setStyleSheet(f"color:{C_DIM};")
            value_lbl.setFont(font(26, QFont.Bold))
            value_lbl.setStyleSheet(f"color:{color};")
            cl.addWidget(tit); cl.addWidget(value_lbl)
            row.addWidget(card)
        lay.addLayout(row)

        # 下面左右两张卡：左边 checklist，右边最近的活
        twocol = QHBoxLayout(); twocol.setSpacing(16)

        # 还差几步就能用（左，宽一点）
        chk = QFrame(); chk.setObjectName("card")
        cl = QVBoxLayout(chk); cl.setContentsMargins(20, 18, 20, 18); cl.setSpacing(6)
        th = QLabel("还差几步就能用"); th.setFont(font(13, QFont.DemiBold))
        cl.addWidget(th)
        sub = QLabel("按顺序配完，从聊天软件里就能直接派活。")
        sub.setFont(font(11)); sub.setStyleSheet(f"color:{C_DIM2};")
        cl.addWidget(sub)
        self._ov_checks = QVBoxLayout()
        self._ov_checks.setSpacing(6)
        cl.addLayout(self._ov_checks)
        twocol.addWidget(chk, 2)

        # 最近的活（右）
        recent = QFrame(); recent.setObjectName("card")
        rl = QVBoxLayout(recent); rl.setContentsMargins(20, 18, 20, 18); rl.setSpacing(8)
        rh = QLabel("最近的活"); rh.setFont(font(13, QFont.DemiBold))
        rl.addWidget(rh)
        rs = QLabel("跑完一批，自动推到聊天软件里。")
        rs.setFont(font(11)); rs.setStyleSheet(f"color:{C_DIM2};")
        rl.addWidget(rs)
        self._ov_recent = QVBoxLayout(); self._ov_recent.setSpacing(6)
        rl.addLayout(self._ov_recent)
        twocol.addWidget(recent, 1)

        lay.addLayout(twocol)
        lay.addStretch()
        self._populate_overview()
        return page

    def _refresh_overview_numbers(self) -> None:
        """三个大数字：正在跑 / 今天完成 / 今日总花销。

        ## 花销是怎么算的

            ⚠️ 原来叫「今天花掉」，而且那个数是**估**出来的：
              读会话文件体积 ÷ 1.6、乘 DeepSeek 的价、永远按未命中缓存。

                · 只认一种价（用户配了 Kimi / 通义就全错）
                · 有缓存命中时估贵十倍
                · 会话体积 ≠ 实际用量

            现在：每一次模型调用按接口回来的 usage 计价
                  （标准 OpenAI 三段式：未命中 / 命中缓存 / 输出），
                  累加在账本里，这里直接读。
                  所有配了的模型都算进来 —— 它就是个总数。
        """
        tasks = getattr(self, "tasks", None) or []

        running = sum(1 for t in tasks if t.get("status") == "running")
        try:
            self._ov_running.setText(str(running))
        except Exception:  # noqa: BLE001
            pass

        # 今天完成：按 finished_at 的日期算
        import time as _t

        today = _t.strftime("%Y-%m-%d")
        done = 0
        for t in tasks:
            if t.get("status") != "ok":
                continue
            fa = str(t.get("finished_at") or "")
            if fa.startswith(today):
                done += 1
        try:
            self._ov_done.setText(str(done))
        except Exception:  # noqa: BLE001
            pass

        # 今日总花销：走本地账本（所有模型加起来）
        try:
            from engine import pricing

            info = pricing.today()
            cny = float(info.get("cny") or 0)
            usd = float(info.get("usd") or 0)

            # ⚠️ 小数位要跟着金额大小走。
            #
            #    实测：一次小任务的真实花销是 $0.000177 ≈ ¥0.0013，
            #    用 "{:.2f}" 格式化出来就是 **¥0.00** —— 看着像没花钱。
            #
            #    按金额分档：
            #        >= 1 元        两位小数   ¥12.34
            #        >= 0.01 元     三位       ¥0.123
            #        再小           显示到分以下 ¥0.0013
            #        真的 0         就写 ¥0
            if cny <= 0:
                self._ov_cost.setText("¥0")
            elif cny >= 1:
                self._ov_cost.setText(f"¥{cny:.2f}")
            elif cny >= 0.01:
                self._ov_cost.setText(f"¥{cny:.3f}")
            else:
                self._ov_cost.setText(f"¥{cny:.4f}")

            # 鼠标停上去看明细：美元、汇率、调用次数、各模型分别多少
            bits = [
                f"美元 ${usd:.6f}",
                f"汇率 {info.get('rate', 0)}",
                f"{info.get('calls', 0)} 次调用",
                f"{info.get('tokens', 0)} tokens",
            ]
            by = info.get("by_model") or {}
            if by:
                bits.append("")
                bits.append("按模型：")
                for m, u in sorted(by.items(), key=lambda x: -x[1]):
                    bits.append(f"  {m}: ${u:.6f}")
            self._ov_cost.setToolTip("\n".join(bits))
        except Exception:  # noqa: BLE001
            self._ov_cost.setText("—")

    def _check_row(self, box: QVBoxLayout, dot_color: str, title: str, detail: str,
                   symbol: str = "dot") -> None:
        """checklist 一行：圆形状态徽标（对勾/感叹号/圆点）+ 粗标题 + 淡说明。"""
        row = QFrame()
        rl = QHBoxLayout(row); rl.setContentsMargins(2, 4, 2, 4); rl.setSpacing(10)
        if symbol == "dot":
            badge = QLabel("●")
            badge.setFixedSize(18, 18)
            badge.setAlignment(Qt.AlignCenter)
            badge.setStyleSheet(f"color:{dot_color}; background:transparent; font-size:13px;")
        else:
            mark = "✓" if symbol == "check" else "!"
            badge = QLabel(mark)
            badge.setFixedSize(18, 18)
            badge.setAlignment(Qt.AlignCenter)
            badge.setStyleSheet(
                f"background:{dot_color}; color:{C_ACCENT_TXT}; border-radius:9px;"
                f" font-size:11px; font-weight:700;"
            )
        col = QVBoxLayout(); col.setSpacing(0); col.setContentsMargins(0, 0, 0, 0)
        t = QLabel(title); t.setFont(font(11.5, QFont.DemiBold))
        d = QLabel(detail); d.setFont(font(10.5)); d.setStyleSheet(f"color:{C_DIM2};")
        d.setWordWrap(True)
        col.addWidget(t); col.addWidget(d)
        rl.addWidget(badge, alignment=Qt.AlignTop); rl.addLayout(col, 1)
        box.addWidget(row)

    def _populate_overview(self) -> None:
        """按真实配置填 checklist；最近的活读任务数据。"""
        # ── 上面那三个大数字 ──
        #
        # ⚠️ 原来这一段只画了 checklist 和"最近的活"，
        #    **三个数字从来没被更新过** —— 永远是 0 / 0 / ¥0.00。
        #    （2026-11-05 老爸问"今天花费能不能拿到真实数据"才发现的。）
        self._refresh_overview_numbers()

        # 清旧
        for box in (self._ov_checks, self._ov_recent):
            while box.count():
                it = box.takeAt(0)
                w = it.widget()
                if w:
                    w.deleteLater()

        # ── 模型 ──
        #
        # ⚠️ 必须**直接读配置**，不能看 self._profiles。
        #
        #    页面是懒建的，概览是第一个建的 ——
        #    这时候模型页还没建，self._profiles 还是空的。
        #    原来拿它判断，于是概览永远显示「还没选模型」，
        #    哪怕用户明明配了两套。
        #
        #    （2026-11-05 截图看到的：下面四项里三项都误报成"没配"。）
        profiles = []
        active_ok = False
        try:
            from tools import userdirs

            _eng = userdirs.config_file("engine.json")
            with open(_eng, encoding="utf-8") as f:
                _ej = json.load(f)
            _all = [p for p in (_ej.get("profiles") or []) if p.get("enabled", True)]
            profiles = _all

            # ⚠️ 光有 profiles 不够，还得有 active。
            #
            #    用户没在模型页点过任何一行时，engine.json 里 active 可能是空的。
            #    而 atrium 的前台**借的就是 active 那套** ——
            #    active 空 → 借不到 → 前台没有模型 →
            #    所有消息都当派活处理（连句"你好"都派出去）。
            #
            #    （profiles.py:317 有个兜底：新增第一套时自动设 active。
            #      但那是走界面加的才触发；手改配置文件不一定有。）
            _act = str(_ej.get("active") or "")
            active_ok = bool(_act) and any(
                str(p.get("id") or "") == _act for p in _all
            )
        except (OSError, json.JSONDecodeError):
            profiles = getattr(self, "_profiles", None) or []
            active_ok = bool(profiles)

        if profiles and active_ok:
            self._check_row(self._ov_checks, C_OK, "模型配好了",
                            f"共 {len(profiles)} 套，随时能切换。", symbol="check")
        elif profiles and not active_ok:
            # 有模型但没指定"当前用哪套" —— 前台借不到，得点一行
            self._check_row(
                self._ov_checks, C_ACCENT, "还没选定当前模型",
                "去「模型」页点一下要用那行，前台和派活都跟着它走。",
                symbol="warn",
            )
        else:
            self._check_row(self._ov_checks, C_ACCENT, "还没选模型",
                            "去「模型」页填一套，主力备用都行。", symbol="warn")

        # ── 验证码邮箱 ──
        # 同样：直接读 guard.json，不看安全页的输入框（那时候还没建）。
        email = ""
        try:
            _g = userdirs.config_file("guard.json")
            with open(_g, encoding="utf-8") as f:
                email = str((json.load(f) or {}).get("email") or "")
        except (OSError, json.JSONDecodeError):
            email = ""

        if email:
            # ⚠️ 这里**不显示邮箱原文**。概览是给人看的，
            #    但同一个窗口也可能被截图分享出去 —— 只显示"配好了"。
            self._check_row(self._ov_checks, C_OK, "验证码邮箱配好了",
                            "要码的时候会发到你设置的那个邮箱。",
                            symbol="check")
        else:
            self._check_row(self._ov_checks, C_ACCENT, "验证码邮箱还没填",
                            "升权限、联网放行时，码要发到这个邮箱。", symbol="warn")

        # atrium 飞书 / 钉钉
        #
        # ⚠️ **概览页不显示任何 app_id / client_id 原文。**
        #
        #    原来是 `fs.get("app_id")` 直接当说明文字往界面上放，
        #    截图上就是 cli_xxxxxxxxxxxxxxxx、dingxxxxxxxxxxxxxxxx。
        #
        #    这两个虽然不是密钥，但它是"该去撞哪个应用"的线索 ——
        #    概览页恰恰是最容易被截图发出去的一页。
        #
        #    （2026-11-05 老爸在截图里一眼看见，说"有明文宝贝"。）
        #
        #    做法跟审计员 Key、验证码邮箱一致：
        #        只说"填了没" + 尾号后 4 位，永远不给完整值。
        at = self._atrium_read()
        plats = at.get("platforms", {}) or {}
        fs = plats.get("feishu", {}) or {}
        dt = plats.get("dingtalk", {}) or {}

        def _tail(v) -> str:
            s = str(v or "")
            return s[-4:] if len(s) > 4 else ""

        if fs.get("app_id"):
            self._check_row(self._ov_checks, C_OK, "飞书通道已填",
                            f"凭据已配（尾号 {_tail(fs.get('app_id'))}）",
                            symbol="check")
        else:
            self._check_row(self._ov_checks, C_BAD, "飞书通道还没填凭据",
                            "在「通道」里填 app_id / app_secret。", symbol="warn")

        if dt.get("enabled") and dt.get("client_id"):
            self._check_row(self._ov_checks, C_OK, "钉钉通道已开",
                            f"凭据已配（尾号 {_tail(dt.get('client_id'))}）",
                            symbol="check")
        else:
            self._check_row(self._ov_checks, C_BAD, "钉钉通道没开",
                            "只用飞书可以不管；要用钉钉去「通道」打开。", symbol="warn")

        # 最近的活
        #
        # ⚠️ 原来这里是 `tasks[:4]`，靠列表顺序。
        #    而列表顺序是 updated_at 倒序 ——
        #    一条老任务重跑，就跑最新的前面去了。
        #    现在按 created_at（派出时间）自己排一遍，
        #    拿"最新派出的 4 条"。
        tasks = getattr(self, "tasks", None) or []
        if not tasks:
            e = QLabel("还没有派过活。从聊天软件里发一句话，它就出现在这儿。")
            e.setFont(font(10.5)); e.setWordWrap(True); e.setStyleSheet(f"color:{C_DIM2};")
            self._ov_recent.addWidget(e)
            return

        newest = sorted(
            tasks,
            key=lambda t: str(t.get("created_at") or t.get("updated_at") or ""),
            reverse=True,
        )[:4]
        for t in newest:
            self._recent_task_row(t)

    def _recent_task_row(self, t: dict) -> None:
        row = QFrame()
        rl = QHBoxLayout(row); rl.setContentsMargins(2, 4, 2, 4)
        col = QVBoxLayout(); col.setSpacing(0)
        nm = QLabel(str(t.get("name") or "?")); nm.setFont(font(11.5, QFont.DemiBold))
        goal = QLabel(str(t.get("goal") or "")); goal.setFont(font(10.5))
        goal.setStyleSheet(f"color:{C_DIM2};")
        col.addWidget(nm); col.addWidget(goal)
        st = t.get("status") or ""
        stmap = {"done": (C_OK, "完成"), "running": (C_ACCENT, "运行中"),
                 "failed": (C_BAD, "失败")}
        c, txt = stmap.get(st, (C_DIM2, st))
        pill = QLabel(txt); pill.setStyleSheet(
            f"color:{c}; background:rgba(255,255,255,0.04); border-radius:999px;"
            f"padding:3px 10px; font-size:10.5px;")
        rl.addLayout(col, 1); rl.addWidget(pill, alignment=Qt.AlignTop)
        self._ov_recent.addWidget(row)

    def _build_channels_page(self) -> QWidget:
        """通道：atrium 的飞书/钉钉凭据 + 前台模型 + 连任务端。

        设计稿：飞书/钉钉两张卡并排；下面「前台 Agent 用哪套模型」、
        「atrium 连到任务端」两张通栏卡。
        """
        page, lay = self._scroll_page()
        # 顶部琥珀说明条
        lay.addWidget(self._info_banner(
            "atrium 是「门」：聊天软件里的消息从这儿进来。两个平台各填各的，只用哪个开哪个。"))

        # 飞书 / 钉钉 并排
        plat = QHBoxLayout(); plat.setSpacing(16)
        plat.addWidget(self._platform_card(
            "飞书", "未连接", "feishu",
            ("App ID", "App Secret"),
            ("cli_xxx", "留空表示不改"),
            "纯网络连接，很轻。扫码或填凭据都行。"))
        plat.addWidget(self._platform_card(
            "钉钉", "未开启", "dingtalk",
            ("Client ID", "Client Secret"),
            ("dingxxx", "留空表示不改"),
            "Stream 长连接收消息，sessionWebhook 回消息。"))
        lay.addLayout(plat)

        # 前台 Agent 用哪套模型
        #
        # ⚠️ 这一块原来有两个死按钮（没 connect），点了没有任何反应。
        #
        # ## atrium 到底怎么选前台模型（看 core/app.py:139 的实现）
        #
        #     优先读 config.json 里的 `front` 段：
        #         front.base_url + front.api_key + front.model
        #         → 三样都填了，就用它
        #     没填就**借任务端那套**（engine.json 里 active 的那个）。
        #
        # ## 所以界面给两个选项
        #
        #     借任务端（默认）   什么都不配，跟着「当前默认那套」走
        #     单独一套           自己填 base_url / Key / 模型名
        #
        #     前者省事，后者可以让前台用便宜模型干活、贵模型留给难活。
        #
        # ## ⚠️ 为什么"借任务端"不指定具体哪一套
        #
        #     因为 atrium 借的就是 active 那套 ——
        #     在「模型」页点哪一行，哪个就是 active。
        #     这里再放一个"选哪套"的下拉，会跟模型页打架：
        #     两边都能改，用户分不清以谁为准。
        fm = QFrame(); fm.setObjectName("card")
        fml = QVBoxLayout(fm); fml.setContentsMargins(20, 18, 20, 18); fml.setSpacing(8)
        fth = QLabel("前台 Agent 用哪套模型"); fth.setFont(font(13, QFont.DemiBold))
        fml.addWidget(fth)
        fs = QLabel(
            "atrium 收到消息后，先有个模型判断「这是闲聊还是要派活」。"
            "默认借任务端当前那套；也可以给它单独配一套（比如用便宜的判意图）。"
        )
        fs.setFont(font(11)); fs.setStyleSheet(f"color:{C_DIM2};"); fs.setWordWrap(True)
        fml.addWidget(fs)

        frow = QHBoxLayout(); frow.setSpacing(8)
        self.btn_fm_borrow = QPushButton("借任务端（推荐）")
        self.btn_fm_borrow.setObjectName("primary")
        self.btn_fm_borrow.clicked.connect(lambda: self._front_mode("borrow"))
        self.btn_fm_manual = QPushButton("单独一套")
        self.btn_fm_manual.setObjectName("ghost")
        self.btn_fm_manual.clicked.connect(lambda: self._front_mode("own"))
        frow.addWidget(self.btn_fm_borrow); frow.addWidget(self.btn_fm_manual)
        frow.addStretch()
        fml.addLayout(frow)

        # 「单独一套」的表单（默认藏着，选了 own 才显示）
        self.front_form = QWidget(fm)
        ffl = QVBoxLayout(self.front_form)
        ffl.setContentsMargins(0, 6, 0, 0)
        ffl.setSpacing(6)

        lb = QLabel("接口地址"); lb.setFont(font(11)); lb.setStyleSheet(f"color:{C_DIM};")
        self.ed_front_base = QLineEdit()
        self.ed_front_base.setPlaceholderText("https://api.deepseek.com")
        ffl.addWidget(lb); ffl.addWidget(self.ed_front_base)

        lb2 = QLabel("API Key"); lb2.setFont(font(11)); lb2.setStyleSheet(f"color:{C_DIM};")
        self.ed_front_key = QLineEdit()
        self.ed_front_key.setEchoMode(QLineEdit.Password)
        self.ed_front_key.setPlaceholderText("留空表示不改")
        ffl.addWidget(lb2); ffl.addWidget(self.ed_front_key)

        lb3 = QLabel("模型名"); lb3.setFont(font(11)); lb3.setStyleSheet(f"color:{C_DIM};")
        self.ed_front_model = QLineEdit()
        self.ed_front_model.setPlaceholderText("deepseek-flash")
        ffl.addWidget(lb3); ffl.addWidget(self.ed_front_model)

        # 「单独一套」的保存按钮
        save_row = QHBoxLayout(); save_row.setSpacing(8)
        self.btn_front_save = QPushButton("保存这套")
        self.btn_front_save.setObjectName("primary")
        self.btn_front_save.clicked.connect(self._save_front)
        save_row.addWidget(self.btn_front_save)
        save_row.addStretch()
        ffl.addLayout(save_row)

        self.front_form.hide()
        fml.addWidget(self.front_form)

        self.lbl_front_now = QLabel("")
        self.lbl_front_now.setFont(font(10.5))
        self.lbl_front_now.setStyleSheet(f"color:{C_DIM2};")
        self.lbl_front_now.setWordWrap(True)
        fml.addWidget(self.lbl_front_now)

        lay.addWidget(fm)

        # atrium 连到任务端
        eng = QFrame(); eng.setObjectName("card")
        el = QVBoxLayout(eng); el.setContentsMargins(20, 18, 20, 18); el.setSpacing(8)
        eth = QLabel("atrium 连到任务端"); eth.setFont(font(13, QFont.DemiBold))
        el.addWidget(eth)
        es = QLabel("atrium 自己不干活，它把消息派给 dsh-bridge。这两个位置都对了才行。")
        es.setFont(font(11)); es.setStyleSheet(f"color:{C_DIM2};"); el.addWidget(es)
        l1 = QLabel("任务端代码位置（engine.path）"); l1.setFont(font(11)); l1.setStyleSheet(f"color:{C_DIM};")
        self.ed_engine_path = QLineEdit(); self.ed_engine_path.setPlaceholderText("D:/Project/dsh-bridge-oss")
        el.addWidget(l1); el.addWidget(self.ed_engine_path)
        l2 = QLabel("队列落盘目录（默认就行，改了消息历史会丢）"); l2.setFont(font(11)); l2.setStyleSheet(f"color:{C_DIM};")
        self.ed_queue_dir = QLineEdit(); self.ed_queue_dir.setPlaceholderText("留空 = 默认位置")
        el.addWidget(l2); el.addWidget(self.ed_queue_dir)
        brow3 = QHBoxLayout(); brow3.setSpacing(8)
        self.btn_chan_test = QPushButton("测试通道"); self.btn_chan_test.setObjectName("ghost")
        # ⚠️ 原来这个按钮**没 connect**，点了没有任何反应。
        #    实测（2026-11-05 老爸要求核对通道页时发现）。
        self.btn_chan_test.clicked.connect(self._atrium_test)
        self.btn_atrium_save = QPushButton("保存"); self.btn_atrium_save.setObjectName("primary")
        self.btn_atrium_save.clicked.connect(self._atrium_save_engine)
        brow3.addWidget(self.btn_chan_test); brow3.addWidget(self.btn_atrium_save); brow3.addStretch()
        el.addLayout(brow3)
        lay.addWidget(eng)
        return page

    def _atrium_test(self) -> None:
        """测试通道：**真去连一次飞书 / 钉钉**。

        ## ⚠️ 之前这个按钮是死的

            原来只 connect 了一句"先保存，再去 atrium 目录跑命令"，
            等于什么都没测。
            （2026-11-05 老爸说"测试没用"，就是它。）

        ## 现在怎么测（全部在桌面端做，不动 atrium）

            一、atrium 进程活着没（读心跳文件）
            二、**拿凭据去飞书/钉钉官方接口换 token**
                换到了 = 凭据对、网络通、应用可用
            三、任务端位置对不对（那里有没有 core/bridge.py）

            第二条是真验证 —— atrium 启动时也要干同样的事，
            凭据错它一样连不上。

        ## ⚠️ 网络请求走后台线程

            换 token 最长要十几秒，放主线程界面会冻住。
        """
        d = self._atrium_read()
        plats = d.get("platforms") or {}

        # 先把要测的东西取出来（主线程读配置，很快）
        fs = plats.get("feishu") or {}
        dt = plats.get("dingtalk") or {}
        fs_on = bool(fs.get("app_id")) and fs.get("enabled") is not False
        dt_on = bool(dt.get("client_id")) and dt.get("enabled") is not False

        fs_id = str(fs.get("app_id") or "")
        fs_sec = str(fs.get("app_secret") or "")
        dt_id = str(dt.get("client_id") or "")
        dt_sec = str(dt.get("client_secret") or "")

        eng_path = str((d.get("engine") or {}).get("path") or "")

        self.btn_chan_test.setEnabled(False)
        self.btn_chan_test.setText("测试中…")

        import threading

        def work() -> None:
            lines: list[str] = []
            ok_all = True

            # ── 一、atrium 进程 ──
            try:
                from tools import atrium_proc

                st = atrium_proc.status()
                if st.get("running"):
                    lines.append(f"✅ atrium 在跑（pid {st.get('pid')}，"
                                 f"已 {st.get('uptime')} 秒）")
                else:
                    ok_all = False
                    lines.append("❌ atrium 没在跑 —— 去平台卡上点一下开关，它会帮你重启")
            except Exception as e:  # noqa: BLE001
                ok_all = False
                lines.append(f"❌ 查不到 atrium 状态：{type(e).__name__}")

            # ── 二、真连一次平台 ──
            try:
                from tools import channel_probe

                if fs_on:
                    ok, msg = channel_probe.check_feishu(fs_id, fs_sec)
                    lines.append(("✅ " if ok else "❌ ") + "飞书：" + msg)
                    ok_all = ok_all and ok
                elif fs.get("app_id"):
                    lines.append("⚪ 飞书：填了凭据但关着，没测")
                else:
                    lines.append("⚪ 飞书：没填凭据")

                if dt_on:
                    ok, msg = channel_probe.check_dingtalk(dt_id, dt_sec)
                    lines.append(("✅ " if ok else "❌ ") + "钉钉：" + msg)
                    ok_all = ok_all and ok
                elif dt.get("client_id"):
                    lines.append("⚪ 钉钉：填了凭据但关着，没测")
                else:
                    lines.append("⚪ 钉钉：没填凭据")
            except Exception as e:  # noqa: BLE001
                ok_all = False
                lines.append(f"❌ 连平台时出错：{type(e).__name__}: {e}")

            # ── 三、任务端位置 ──
            import os as _os

            if eng_path and _os.path.exists(_os.path.join(eng_path, "core", "bridge.py")):
                lines.append(f"✅ 任务端位置对：{eng_path}")
            elif eng_path:
                ok_all = False
                lines.append(f"❌ 任务端位置不对（那里没有 core/bridge.py）：{eng_path}")
            else:
                ok_all = False
                lines.append("❌ 没填任务端位置")

            title = "通道通了" if ok_all else "通道有问题"
            body = "\n".join(lines)
            if not fs_on and not dt_on:
                ok_all = False
                title = "通道有问题"
                body += "\n\n⚠️ 一个平台都没开 —— 聊天软件那边发消息过来没人接"

            def apply() -> None:
                try:
                    self.btn_chan_test.setEnabled(True)
                    self.btn_chan_test.setText("测试通道")
                except Exception:  # noqa: BLE001
                    pass
                QMessageBox.information(self, title, body)

            q = getattr(self, "_main_queue", None)
            if q is not None:
                q.append(apply)
            else:
                apply()

        threading.Thread(target=work, daemon=True).start()

    def _platform_card(self, title: str, state: str, key: str,
                       labels: tuple[str, str], placeholders: tuple[str, str],
                       desc: str = "") -> QFrame:
        """飞书/钉钉 那种平台凭据卡。"""
        card = QFrame(); card.setObjectName("card")
        cl = QVBoxLayout(card); cl.setContentsMargins(20, 18, 20, 18); cl.setSpacing(8)
        head = QHBoxLayout(); cl.addLayout(head)
        th = QLabel(title); th.setFont(font(13, QFont.DemiBold)); head.addWidget(th)
        state_lbl = QLabel(state); state_lbl.setFont(font(10.5)); state_lbl.setStyleSheet(f"color:{C_DIM2};")
        head.addStretch(); head.addWidget(state_lbl)
        if desc:
            d = QLabel(desc); d.setFont(font(11)); d.setStyleSheet(f"color:{C_DIM2};")
            d.setWordWrap(True)
            cl.addWidget(d)
        if key == "feishu":
            self.ed_feishu_id = QLineEdit(); self.ed_feishu_id.setPlaceholderText(placeholders[0])
            self.ed_feishu_secret = QLineEdit(); self.ed_feishu_secret.setEchoMode(QLineEdit.Password)
            self.ed_feishu_secret.setPlaceholderText(placeholders[1])
            self._ch_feishu_state = state_lbl
            # 给 _apply_platform_switch 用（关掉平台时禁用输入框）
            self._ed_feishu_id = self.ed_feishu_id
            self._ed_feishu_secret = self.ed_feishu_secret
            idw, secw = self.ed_feishu_id, self.ed_feishu_secret
        else:
            self.ed_dt_id = QLineEdit(); self.ed_dt_id.setPlaceholderText(placeholders[0])
            self.ed_dt_secret = QLineEdit(); self.ed_dt_secret.setEchoMode(QLineEdit.Password)
            self.ed_dt_secret.setPlaceholderText(placeholders[1])
            self._ch_dingtalk_state = state_lbl
            self._ed_dt_id = self.ed_dt_id
            self._ed_dt_secret = self.ed_dt_secret
            idw, secw = self.ed_dt_id, self.ed_dt_secret
        for lab, w in ((labels[0], idw), (labels[1], secw)):
            l = QLabel(lab); l.setFont(font(11)); l.setStyleSheet(f"color:{C_DIM};")
            cl.addWidget(l); cl.addWidget(w)

        # ── 开 / 关 ──
        #
        # ⚠️ 原来**没有这个开关**，保存的时候写死 p["enabled"] = True。
        #    于是想"只开飞书、不开钉钉"做不到：两边都保存过一次，
        #    两个都被设成 True。
        #
        #    atrium 本身是按 enabled 决定起不起的（app.py:102），
        #    逻辑是对的，问题在**界面上没地方把它设成 False**。
        #
        # ⚠️ 按钮上写的是**"点了会做什么"**，不是"现在是什么状态"：
        #
        #        现在是开着的   → 按钮写「关闭」  （点了就关）
        #        现在是关着的   → 按钮写「启用」  （点了就开）
        #
        #    当前状态在卡片右上角那行小字里（_apply_platform_switch 更新）。
        #
        # ⚠️ 样式用 ghost（灰底细边），**不能用 switchon（黄底）**。
        #    黄底在这个界面里是"主要动作"的意思，
        #    开关跟「测试连接」并排时两个都黄，主次全乱。
        sw = QPushButton("关闭")
        sw.setCheckable(True)
        sw.setCursor(Qt.PointingHandCursor)
        sw.setFixedHeight(34)
        sw.setMinimumWidth(78)
        sw.setObjectName("ghost")
        if key == "feishu":
            self._sw_feishu = sw
        else:
            self._sw_dingtalk = sw
        sw.clicked.connect(lambda _=False, k=key: self._atrium_toggle(k))

        # 开关跟右边两个动作分开一截，一眼能看出不是一类
        brow = QHBoxLayout(); brow.setSpacing(8)
        brow.addWidget(sw)
        brow.addSpacing(10)

        test = QPushButton("测试连接")
        test.setObjectName("primary")
        # ⚠️ 这个按钮原来**只创建、没 connect** —— 点了啥也不发生。
        #
        #    （2026-11-05 老爸说"飞书和钉钉测试连接点了，啥效果都没有"。）
        #
        #    注意别跟下面那张卡上的「测试通道」搞混：
        #        平台卡上的「测试连接」  只测**这一个平台**
        #        下面卡的「测试通道」   测三样（atrium / 两个平台 / 任务端）
        if key == "feishu":
            self.btn_test_feishu = test
        else:
            self.btn_test_dingtalk = test
        test.clicked.connect(lambda _=False, k=key: self._test_platform(k))

        save = QPushButton("保存"); save.setObjectName("ghost")
        save.clicked.connect(lambda _=False, k=key: self._atrium_save(k))
        brow.addWidget(test); brow.addWidget(save); brow.addStretch()
        cl.addLayout(brow)

        return card

    def _test_platform(self, key: str) -> None:
        """测单个平台：**拿界面上的凭据真去换一次 token**。

        ## 为什么用界面上的值，不用配置里的

            用户可能刚填完 Key 还没点保存 ——
            这时候拿配置里的旧值去测，测的是上一次的东西，
            结果跟他看到的输入框对不上。

            （跟安全页「发测试邮件」同一个道理：
              那个也特意改成用界面上填的值。）

        ## ⚠️ 没填的先从配置里补

            Secret 那种字段是不回显的（只写"留空表示不改"），
            所以输入框是空的。这时候要用配置里存着的那个。
        """
        label = "飞书" if key == "feishu" else "钉钉"
        btn = getattr(self, "btn_test_feishu" if key == "feishu" else "btn_test_dingtalk", None)

        # 界面上的值
        if key == "feishu":
            typed_id = self.ed_feishu_id.text().strip()
            typed_sec = self.ed_feishu_secret.text().strip()
        else:
            typed_id = self.ed_dt_id.text().strip()
            typed_sec = self.ed_dt_secret.text().strip()

        # 配置里的值（补空）
        d = self._atrium_read()
        p = ((d.get("platforms") or {}).get(key) or {})
        if key == "feishu":
            use_id = typed_id or str(p.get("app_id") or "")
            use_sec = typed_sec or str(p.get("app_secret") or "")
        else:
            use_id = typed_id or str(p.get("client_id") or "")
            use_sec = typed_sec or str(p.get("client_secret") or "")

        if not use_id:
            QMessageBox.warning(self, label, f"先填 {'App ID' if key == 'feishu' else 'Client ID'}。")
            return
        if not use_sec:
            QMessageBox.warning(
                self, label,
                f"缺 Secret。填上，或者先保存一次让配置里存着。",
            )
            return

        if btn is not None:
            btn.setEnabled(False)
            btn.setText("测试中…")

        import threading

        def work() -> None:
            try:
                from tools import channel_probe

                if key == "feishu":
                    ok, msg = channel_probe.check_feishu(use_id, use_sec)
                else:
                    ok, msg = channel_probe.check_dingtalk(use_id, use_sec)
            except Exception as e:  # noqa: BLE001
                ok, msg = False, f"{type(e).__name__}: {e}"

            def apply() -> None:
                if btn is not None:
                    try:
                        btn.setEnabled(True)
                        btn.setText("测试连接")
                    except Exception:  # noqa: BLE001
                        pass
                if ok:
                    QMessageBox.information(self, label, f"✅ {msg}")
                else:
                    QMessageBox.warning(self, label, f"❌ {msg}")

            q = getattr(self, "_main_queue", None)
            if q is not None:
                q.append(apply)
            else:
                apply()

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _info_banner(text: str) -> QFrame:
        """琥珀色提示条（设计稿 .banner.boot）。"""
        b = QFrame()
        b.setStyleSheet(
            f"background:rgba(224,168,60,0.12); border:1px solid rgba(224,168,60,0.30);"
            f"border-radius:10px;")
        l = QVBoxLayout(b); l.setContentsMargins(16, 12, 16, 12)
        t = QLabel(text); t.setFont(font(12)); t.setStyleSheet(f"color:{C_ACCENT};")
        t.setWordWrap(True)
        l.addWidget(t)
        return b

    def _build_models_page(self) -> QWidget:
        """模型：任务端多套模型。"""
        page, lay = self._scroll_page()
        self._card_model(lay)
        return page

    def _build_security_page(self) -> QWidget:
        """安全：安全模式 / 版本回退 / 验证码邮箱 / 审核员。

        设计稿：安全模式通栏；然后版本回退（它是「出事可回退」的底座，
        紧跟在模式下面最有说服力）；再下面验证码邮箱（左2）+ 审核员（右1）。
        """
        page, lay = self._scroll_page()
        self._card_security_mode(lay)
        cols = QHBoxLayout(); cols.setSpacing(16)
        left = QVBoxLayout(); left.setSpacing(0)
        right = QVBoxLayout(); right.setSpacing(0)
        self._card_guard(left)
        self._card_auditor(right)
        cols.addLayout(left, 2); cols.addLayout(right, 1)
        lay.addLayout(cols)
        return page

    def _build_perms_page(self) -> QWidget:
        """权限：窗口权限档 + 联网授权。"""
        page, lay = self._scroll_page()

        # 窗口权限表
        wcard = QFrame(); wcard.setObjectName("card")
        wl = QVBoxLayout(wcard); wl.setContentsMargins(20, 18, 20, 18)
        wh = QLabel("窗口权限"); wh.setFont(font(13, QFont.DemiBold)); wl.addWidget(wh)
        ws = QLabel("每个任务窗口能碰多大地方。放宽要验证码，收窄直接生效。")
        ws.setFont(font(11)); ws.setStyleSheet(f"color:{C_DIM2};"); wl.addWidget(ws)

        # 表头（窗口名/状态/权限档/说明），跟数据行用同一套列宽
        hdr = QHBoxLayout(); hdr.setContentsMargins(8, 4, 8, 4); hdr.setSpacing(10)
        for text, stretch in (("窗口名", 2), ("状态", 1), ("权限档", 1), ("说明", 2)):
            h = QLabel(text); h.setFont(font(10.5)); h.setStyleSheet(f"color:{C_DIM};")
            hdr.addWidget(h, stretch)
        wl.addLayout(hdr)
        hline = QFrame(); hline.setFixedHeight(1); hline.setStyleSheet(f"background:{C_LINE};")
        wl.addWidget(hline)

        # 权限表行多了不能把下面的联网授权卡挤没 —— 限高、内部滚动。
        # 设计稿只画 3 行示意，真机累积了几十条 e2e 测试任务，不限位就撑满整屏。
        perms_scroll = QScrollArea()
        perms_scroll.setWidgetResizable(True)
        perms_scroll.setFrameShape(QFrame.NoFrame)
        perms_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        perms_scroll.setStyleSheet("QScrollArea { border:none; background:transparent; }")
        perms_host = QWidget()
        perms_host.setStyleSheet("background:transparent;")
        self._perms_box = QVBoxLayout(perms_host); self._perms_box.setSpacing(0)
        self._perms_box.setContentsMargins(0, 0, 0, 0)
        perms_scroll.setWidget(perms_host)
        perms_scroll.setFixedHeight(230)
        wl.addWidget(perms_scroll)
        lay.addWidget(wcard)

        # 联网授权（左）+ 放行中的样子（右）并排
        netrow = QHBoxLayout(); netrow.setSpacing(16)

        ncard = QFrame(); ncard.setObjectName("card")
        nl = QVBoxLayout(ncard); nl.setContentsMargins(20, 18, 20, 18); nl.setSpacing(8)
        nth = QLabel("联网授权"); nth.setFont(font(13, QFont.DemiBold)); nl.addWidget(nth)
        ns = QLabel("沙箱默认断网。要装包、下文件时，得你点头放行一段时间。")
        ns.setFont(font(11)); ns.setStyleSheet(f"color:{C_DIM2};"); nl.addWidget(ns)
        # 状态横幅：跟设计稿一样，红/绿圆角底 + 左边圆点，不是裸文字。
        self._net_banner = QWidget()
        self._net_banner.setObjectName("netBanner")
        self._net_dot = QLabel("●")
        self._net_dot.setFixedWidth(16)
        self._net_state = QLabel("正在查询…")
        self._net_state.setWordWrap(True)
        nb = QHBoxLayout(self._net_banner)
        nb.setContentsMargins(14, 10, 14, 10)
        nb.setSpacing(6)
        nb.addWidget(self._net_dot, 0, Qt.AlignTop)
        nb.addWidget(self._net_state, 1)
        self._style_net_banner(None)
        nl.addWidget(self._net_banner)
        self.btn_net_code = QPushButton("申请联网授权"); self.btn_net_code.setObjectName("primary")
        self.btn_net_code.clicked.connect(self._net_request_code)
        nl.addWidget(self.btn_net_code, alignment=Qt.AlignLeft)
        netrow.addWidget(ncard, 1)

        # 示例卡：真到放行那天，这里变成倒计时
        ex = QFrame(); ex.setObjectName("card")
        exl = QVBoxLayout(ex); exl.setContentsMargins(20, 18, 20, 18); exl.setSpacing(8)
        exh = QLabel("放行中的样子（示例）"); exh.setFont(font(13, QFont.DemiBold))
        exl.addWidget(exh)
        exs = QLabel("真到那一天，这里会变成倒计时。")
        exs.setFont(font(11)); exs.setStyleSheet(f"color:{C_DIM2};"); exl.addWidget(exs)
        exb = QWidget()
        exb.setObjectName("netBannerEx")
        exb.setStyleSheet(
            f"QWidget#netBannerEx {{background:rgba(95,198,126,0.14); border:1px solid rgba(95,198,126,0.30);"
            f"border-radius:10px;}}")
        exbl = QHBoxLayout(exb); exbl.setContentsMargins(14, 10, 14, 10); exbl.setSpacing(6)
        exdot = QLabel("●"); exdot.setFixedWidth(16); exdot.setStyleSheet(f"color:{C_OK};")
        exbt = QLabel("已允许联网，还剩 20 分 47 秒"); exbt.setStyleSheet(f"color:{C_OK};")
        exbl.addWidget(exdot, 0, Qt.AlignTop)
        exbl.addWidget(exbt, 1)
        exl.addWidget(exb)
        self.btn_net_revoke = QPushButton("提前收回"); self.btn_net_revoke.setObjectName("ghost")
        self.btn_net_revoke.clicked.connect(self._net_revoke)
        exl.addWidget(self.btn_net_revoke, alignment=Qt.AlignLeft)
        netrow.addWidget(ex, 1)

        lay.addLayout(netrow)
        lay.addStretch()
        return page

    def _build_diagnose_page(self) -> QWidget:
        """诊断：看现场。后端出 JSON，前端画成一行行状态。"""
        page, lay = self._scroll_page()

        # 顶部说明 + 刷新按钮
        head = QHBoxLayout()
        ht = QLabel("跑一轮自检")
        ht.setFont(font(13, QFont.DemiBold))
        head.addWidget(ht)
        head.addStretch()
        btn_refresh = QPushButton("重新检查")
        btn_refresh.setObjectName("ghost")
        btn_refresh.clicked.connect(lambda: self._load_diagnose())
        head.addWidget(btn_refresh)
        lay.addLayout(head)

        sub = QLabel("每项告诉你：通没通、现在什么样、出了问题怎么修。")
        sub.setFont(font(11)); sub.setStyleSheet(f"color:{C_DIM2};")
        lay.addWidget(sub)
        lay.addSpacing(12)

        # 结果列表容器
        self._diag_box = QVBoxLayout()
        self._diag_box.setSpacing(10)
        wrap = QWidget()
        wrap.setLayout(self._diag_box)
        lay.addWidget(wrap)
        lay.addStretch()

        self._load_diagnose()
        return page

    def _load_diagnose(self) -> None:
        """拉 /api/diagnose，画成一行行卡片。"""
        # 清空旧项
        while self._diag_box.count():
            it = self._diag_box.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()

        loading = QLabel("正在检查…")
        loading.setStyleSheet(f"color:{C_DIM2}; padding:20px;")
        self._diag_box.addWidget(loading)

        def done(data):
            # 清掉 loading
            while self._diag_box.count():
                it = self._diag_box.takeAt(0)
                w = it.widget()
                if w:
                    w.deleteLater()

            if not isinstance(data, dict) or "checks" not in data:
                err = QLabel(f"检查失败：{data}")
                err.setStyleSheet(f"color:{C_BAD}; padding:20px;")
                self._diag_box.addWidget(err)
                return

            for c in data["checks"]:
                self._diag_box.addWidget(self._diag_row(c))

        self.api.request_once("/api/diagnose", callback=done)

    def _diag_row(self, c: dict) -> QWidget:
        """单条诊断结果：左边状态点 + 名称/详情/修复提示。"""
        row = QFrame(); row.setObjectName("card")
        rl = QHBoxLayout(row); rl.setContentsMargins(16, 14, 16, 14); rl.setSpacing(14)

        # 状态点
        dot = QLabel("●")
        dot.setFixedWidth(16)
        color = C_OK if c.get("ok") else C_BAD
        dot.setStyleSheet(f"color:{color}; font-size:14px;")
        rl.addWidget(dot, 0, Qt.AlignTop)

        # 中间：名称 + 详情 + 修复提示
        mid = QVBoxLayout(); mid.setSpacing(2)
        name = QLabel(c.get("label", ""))
        name.setFont(font(11.5, QFont.DemiBold)); name.setStyleSheet(f"color:{C_TEXT};")
        mid.addWidget(name)
        detail = QLabel(c.get("detail", ""))
        detail.setFont(font(10.5)); detail.setStyleSheet(f"color:{C_DIM2};")
        detail.setWordWrap(True)
        mid.addWidget(detail)
        fix = c.get("fix") or ""
        if fix:
            fix_lb = QLabel("→ " + fix)
            fix_lb.setFont(font(10.5)); fix_lb.setStyleSheet(f"color:{C_ACCENT};")
            fix_lb.setWordWrap(True)
            mid.addWidget(fix_lb)
        rl.addLayout(mid, 1)

        return row

    def _build_system_page(self) -> QWidget:
        """系统：依赖 + 启动（开机自启/版本）。跟设计稿对齐。"""
        page, lay = self._scroll_page()

        # ── 卡 1：依赖 ──
        self._dep_rows = {}
        self._installing = False
        dcard = QFrame(); dcard.setObjectName("card")
        dl = QVBoxLayout(dcard); dl.setContentsMargins(20, 18, 20, 18); dl.setSpacing(6)
        dh = QLabel("依赖"); dh.setFont(font(13, QFont.DemiBold)); dl.addWidget(dh)
        ds = QLabel("跑起来需要的东西，缺哪个点「装它」。")
        ds.setFont(font(11)); ds.setStyleSheet(f"color:{C_DIM2};"); dl.addWidget(ds)
        dl.addSpacing(8)

        self.deps_box = QVBoxLayout(); self.deps_box.setSpacing(0)
        self.deps_box.setContentsMargins(0, 0, 0, 0)
        dl.addLayout(self.deps_box)
        lay.addWidget(dcard)

        # ── 卡 2：启动 ──
        scard = QFrame(); scard.setObjectName("card")
        sl = QVBoxLayout(scard); sl.setContentsMargins(20, 18, 20, 18); sl.setSpacing(0)
        st = QLabel("启动"); st.setFont(font(13, QFont.DemiBold)); sl.addWidget(st)
        sl.addSpacing(10)

        # 开机自启行
        srow1 = QHBoxLayout(); srow1.setSpacing(12); srow1.setContentsMargins(0, 4, 0, 12)
        scol1 = QVBoxLayout(); scol1.setSpacing(2)
        s1t = QLabel("开机自启"); s1t.setFont(font(11.5, QFont.DemiBold))
        s1s = QLabel("开机时自动把任务台启动起来")
        s1s.setFont(font(10.5)); s1s.setStyleSheet(f"color:{C_DIM2};")
        scol1.addWidget(s1t); scol1.addWidget(s1s)
        srow1.addLayout(scol1, 1)
        self.switch_auto = QPushButton("开机自启 · 关")
        self.switch_auto.setCheckable(True)
        self.switch_auto.setCursor(Qt.PointingHandCursor)
        self.switch_auto.setMinimumWidth(120)
        self.switch_auto.clicked.connect(self._toggle_autostart)
        srow1.addWidget(self.switch_auto, 0, Qt.AlignTop)
        sl.addLayout(srow1)

        # 分隔线
        div = QFrame(); div.setFixedHeight(1); div.setStyleSheet(f"background:{C_LINE}; border:none;")
        sl.addWidget(div)

        # 版本行
        srow2 = QHBoxLayout(); srow2.setSpacing(12); srow2.setContentsMargins(0, 12, 0, 4)
        scol2 = QVBoxLayout(); scol2.setSpacing(2)
        s2t = QLabel("版本"); s2t.setFont(font(11.5, QFont.DemiBold))
        s2s = QLabel(self._read_version_text())
        s2s.setFont(font(10.5)); s2s.setStyleSheet(f"color:{C_DIM2};")
        scol2.addWidget(s2t); scol2.addWidget(s2s)
        srow2.addLayout(scol2, 1)
        self.btn_check_update = QPushButton("检查更新")
        self.btn_check_update.setObjectName("ghost")
        self.btn_check_update.setCursor(Qt.PointingHandCursor)
        self.btn_check_update.clicked.connect(self._check_updates)
        srow2.addWidget(self.btn_check_update, 0, Qt.AlignTop)
        sl.addLayout(srow2)

        lay.addWidget(scard)
        lay.addStretch()

        # 刷依赖列表 + 刷自启状态
        self._refresh_deps()
        self._refresh_autostart()
        return page

    def refresh(self) -> None:
        # 后台还没好 / 窗口最小化或被藏起来 —— 这一轮不发请求。
        if not self._backend_ready:
            return
        if self.isMinimized() or not self.isVisible():
            return
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
            # 概览页上的三张卡和"最近的活"也要跟着走。
            #
            # ⚠️ 原来 _populate_overview 只在**建页面时**调一次 ——
            #    那一刻 tasks 还是空的，于是"最近的活"永远显示
            #    「还没有派过活」，数字也永远是 0。
            #
            #    现在每次任务数据回来都刷一遍（只是改几行文字，
            #    不重建控件，开销很小）。
            #    概览页还没建（用户没点过去）就跳过。
            if getattr(self, "_ov_checks", None) is not None:
                self._populate_overview()
        elif path == "/api/services" and isinstance(data, dict):
            for k, dot in self.pills.items():
                dot.set_color(C_OK if data.get(k) else C_BAD)
            for k, card in getattr(self, "svc_cards", {}).items():
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
                self.switch(3)
            return

        # 其他情况：原因和提示都摆出来
        msg = err if not hint else f"{err}\n\n{hint}"
        QMessageBox.warning(self, "没成", msg)

    # ── 权限页 ──
    def _level_desc(self, level: str) -> str:
        """权限档对应的一句话说明（设计稿里的"说明"列）。"""
        return {
            "workspace": "只能动它自己那个任务目录",
            "home": "能动文档、桌面、下载",
            "full": "整个电脑都能动",
        }.get(level, "—")

    def _load_perms(self) -> None:
        """拉窗口权限列表，画成一行行。"""
        def done(data):
            if not isinstance(data, dict):
                return
            # 清空旧行
            while self._perms_box.count():
                it = self._perms_box.takeAt(0)
                w = it.widget()
                if w:
                    w.deleteLater()
            for t in data.get("tasks") or []:
                row = QFrame()
                rl = QHBoxLayout(row); rl.setContentsMargins(8, 9, 8, 9); rl.setSpacing(10)
                nm = QLabel(str(t.get("name") or "?"))
                nm.setStyleSheet(f"color:{C_TEXT};")
                cmb = QComboBox()
                levels = data.get("levels") or ["workspace", "home", "full"]
                labels = data.get("labels") or {}
                for lv in levels:
                    cmb.addItem(labels.get(lv, lv), lv)
                cur = t.get("level") or "workspace"
                i = cmb.findData(cur)
                if i >= 0:
                    cmb.setCurrentIndex(i)
                cmb.currentIndexChanged.connect(
                    lambda _=False, name=t.get("name"), c=cmb: self._perm_set(name, c.currentData())
                )
                st = QLabel({"ok": "完成", "running": "运行中", "error": "出错"}.get(t.get("status"), t.get("status") or ""))
                st.setStyleSheet(f"color:{C_DIM2};")
                desc = QLabel(self._level_desc(cur))
                desc.setWordWrap(True); desc.setStyleSheet(f"color:{C_DIM2};")
                rl.addWidget(nm, 2); rl.addWidget(st, 1); rl.addWidget(cmb, 1); rl.addWidget(desc, 2)
                self._perms_box.addWidget(row)
            if not (data.get("tasks")):
                empty = QLabel("还没有任务窗口。派一个活，它就出现在这儿。")
                empty.setStyleSheet(f"color:{C_DIM2}; padding:8px;")
                self._perms_box.addWidget(empty)

        self.api.request_once("/api/perms", callback=done)

    def _perm_set(self, name: str, level: str) -> None:
        """改某个窗口的权限档。升档要码，后端会回 need_code。"""
        def done(data):
            if not isinstance(data, dict):
                return
            if data.get("ok"):
                QMessageBox.information(self, "权限", data.get("label") or "已调整")
            elif data.get("need_code"):
                code, ok = QInputDialog.getText(
                    self, "要升权限",
                    f"{data.get('message') or '升到这一档需要验证码'} 收到的码：")
                if ok and code.strip():
                    self.api.post("/api/perms", {"name": name, "level": level, "code": code.strip()})
            else:
                QMessageBox.warning(self, "没成", data.get("error") or data.get("message") or "调整失败")
            self._load_perms()

        self.api.request_once("/api/perms", {"name": name, "level": level}, callback=done)

    def _style_net_banner(self, kind):
        """kind: 'ok' 放行 / 'bad' 断网 / None 查询中。统一横幅+圆点配色。"""
        if kind == "ok":
            bg, border, fg = "rgba(95,198,126,0.14)", "rgba(95,198,126,0.30)", C_OK
        elif kind == "bad":
            bg, border, fg = "rgba(232,106,99,0.14)", "rgba(232,106,99,0.30)", C_BAD
        else:
            bg, border, fg = "rgba(138,146,158,0.10)", "rgba(138,146,158,0.25)", C_DIM
        self._net_banner.setStyleSheet(
            f"QWidget#netBanner {{background:{bg}; border:1px solid {border}; border-radius:10px;}}")
        self._net_dot.setStyleSheet(f"color:{fg};")
        self._net_state.setStyleSheet(f"color:{fg};")

    def _load_net_status(self) -> None:
        def done(data):
            if not isinstance(data, dict):
                return
            if data.get("granted"):
                left = int(data.get("remaining") or 0)
                mm, ss = divmod(left, 60)
                self._net_state.setText(f"已允许联网，还剩 {mm} 分 {ss:02d} 秒")
                self._style_net_banner("ok")
            else:
                why = data.get("why") or "现在不允许联网 —— 任务要联网时会被拦下来"
                self._net_state.setText(why)
                self._style_net_banner("bad")
        self.api.request_once("/api/net/status", callback=done)

    def _net_request_code(self) -> None:
        """申请联网：先让后端把码发到邮箱，再问用户要码。"""
        def sent(data):
            if isinstance(data, dict) and data.get("ok"):
                code, ok = QInputDialog.getText(self, "联网授权", "码发到邮箱了。把码填进来：")
                if not (ok and code.strip()):
                    return
                def allowed(d2):
                    if isinstance(d2, dict) and d2.get("ok"):
                        QMessageBox.information(self, "联网", "已放行联网")
                    else:
                        QMessageBox.warning(self, "没成", (d2 or {}).get("error") or "验码失败")
                    self._load_net_status()
                self.api.request_once(
                    "/api/net/allow", {"code": code.strip(), "minutes": 30}, callback=allowed)
            else:
                QMessageBox.warning(self, "没成", (data or {}).get("message") or "发码失败")
        self.api.request_once("/api/net/code", callback=sent)

    def _net_revoke(self) -> None:
        def done(data):
            self._load_net_status()
        self.api.request_once("/api/net/revoke", callback=done)

    # ── 通道页（atrium，直接读写 ~/.atrium/config.json）──
    @staticmethod
    def _atrium_config_path() -> str:
        """atrium 的配置文件在哪。

        ⚠️ **在用户主目录下：~/.atrium/config.json**

            原来这里写的是：
                if sys.platform == "win32":
                    base = os.environ.get("APPDATA") or os.path.expanduser("~")
                else:
                    base = os.path.expanduser("~")
                return os.path.join(base, ".atrium", "config.json")

            而注释还写着「Windows 上 APPDATA 是漫游目录，和家目录等价」——
            **不等价。**
                APPDATA          = C:\\Users\\xxx\\AppData\\Roaming
                用户主目录        = C:\\Users\\xxx

            APPDATA 是主目录的子目录，差了一层。
            拼出来的路径永远是
                C:\\Users\\xxx\\AppData\\Roaming\\.atrium\\config.json
            而 atrium 实际写在
                C:\\Users\\xxx\\.atrium\\config.json

            于是概览页永远报「飞书通道还没填凭据」。

            （2026-11-05 截图看到的：明明配好了，界面上是红的。
              同一个错在服务端的 /api/diagnose 里也有一份，一起修了。）

        atrium 自己在 core/config.py 里用的就是 os.path.expanduser("~")，
        跟平台无关。我们也照它的来。
        """
        return os.path.join(os.path.expanduser("~"), ".atrium", "config.json")

    def _atrium_read(self) -> dict:
        p = self._atrium_config_path()
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _atrium_write(self, d: dict) -> None:
        p = self._atrium_config_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)

    def _load_atrium(self) -> None:
        d = self._atrium_read()
        fs = d.get("platforms", {}).get("feishu", {}) or {}
        dt = d.get("platforms", {}).get("dingtalk", {}) or {}
        eng = d.get("engine", {}) or {}
        if fs.get("app_id"):
            self.ed_feishu_id.setText(fs["app_id"])
        if dt.get("client_id"):
            self.ed_dt_id.setText(dt["client_id"])
        if eng.get("path"):
            self.ed_engine_path.setText(eng["path"])

        # 开关画成真实状态
        #
        # ⚠️ 原来只写「已配置 / 未开启」，但那个"已配置"看的是 enabled，
        #    而 enabled 永远被设成 True —— 所以永远显示"已配置"，
        #    用户看不出到底开没开。
        self._apply_platform_switch("feishu", fs)
        self._apply_platform_switch("dingtalk", dt)

        # 队列落盘目录
        qdir = (d.get("storage") or {}).get("dir") or ""
        ed_q = getattr(self, "ed_queue_dir", None)
        if ed_q is not None and qdir:
            ed_q.setText(str(qdir))

        # 前台用哪套模型
        #
        # ⚠️ atrium 那边读的是 front.base_url / front.api_key / front.model
        #    三个都有才用自己这套，否则借任务端 active 那套。
        #
        #    所以界面上：
        #        配了 front 段      → 表单显示出来，按钮高亮「单独一套」
        #        没配               → 表单藏起来，按钮高亮「借任务端」
        f = d.get("front") or {}
        has_own = bool(f.get("base_url") and f.get("api_key") and f.get("model"))

        # 借用的那套到底在不在（engine.json 里有 active 且那个 profile 存在）
        #
        # ⚠️ 这一步决定"按钮要不要高亮"。
        #
        #    老爸的原话：「没有填写的时候，借用就不能高亮，并且要给提示」
        #
        #    原来：没配 front 段 → 「借任务端」直接黄底高亮，
        #          看着像"用户已经选好了"，其实什么都没配。
        #          更糟的是如果 engine.json 里连 active 都没有，
        #          借用根本借不到东西，前台是空的 —— 界面还是绿的。
        borrow_name = ""
        borrow_ok = False
        try:
            from tools import userdirs

            with open(userdirs.config_file("engine.json"), encoding="utf-8") as fp:
                ej = json.load(fp)
            act = str(ej.get("active") or "")
            for pp in ej.get("profiles") or []:
                if not isinstance(pp, dict):
                    continue
                if pp.get("enabled") is False:
                    continue
                if str(pp.get("id") or "") == act:
                    borrow_name = f"{pp.get('name')}｜{pp.get('model')}"
                    borrow_ok = True
                    break
        except (OSError, json.JSONDecodeError):
            pass

        form = getattr(self, "front_form", None)
        if form is not None:
            form.setVisible(has_own)

        b1 = getattr(self, "btn_fm_borrow", None)
        b2 = getattr(self, "btn_fm_manual", None)
        lbl = getattr(self, "lbl_front_now", None)

        if has_own:
            # ── 配了自己那套：高亮「单独一套」 ──
            for w, v in (
                (getattr(self, "ed_front_base", None), f.get("base_url") or ""),
                (getattr(self, "ed_front_model", None), f.get("model") or ""),
            ):
                if w is not None:
                    w.setText(str(v))
            ed_k = getattr(self, "ed_front_key", None)
            if ed_k is not None:
                ed_k.clear()
                ed_k.setPlaceholderText("已保存 · 要换就输入新的")

            if b1 is not None:
                b1.setObjectName("ghost")
            if b2 is not None:
                b2.setObjectName("primary")

            if lbl is not None:
                lbl.setText(f"现在用：{f.get('model')} ＠ {f.get('base_url')}")
                lbl.setStyleSheet(f"color:{C_OK}; font-size:10.5px;")

        elif borrow_ok:
            # ── 没配自己那套，但任务端有 active 可以借：高亮「借任务端」 ──
            if b1 is not None:
                b1.setObjectName("primary")
            if b2 is not None:
                b2.setObjectName("ghost")

            if lbl is not None:
                lbl.setText(f"现在借任务端的：{borrow_name}")
                lbl.setStyleSheet(f"color:{C_OK}; font-size:10.5px;")

        else:
            # ── **两样都没有：两个按钮都不高亮，明确告诉用户要配东西** ──
            #
            #   这种情况真要命：atrium 起来会发现"没有前台模型配置"，
            #   然后**所有消息都当派活处理**（连"你好"都派出去）。
            #   用户却看着界面一片正常。
            if b1 is not None:
                b1.setObjectName("ghost")
            if b2 is not None:
                b2.setObjectName("ghost")

            if lbl is not None:
                lbl.setText(
                    "⚠️ 还没配前台模型：任务端一套都没有，"
                    "atrium 会把你说的话全当派活。\n"
                    "先去「模型」页加一套（前台会跟着它走），"
                    "或者点上面「单独一套」自己填。"
                )
                lbl.setStyleSheet(f"color:{C_ACCENT}; font-size:10.5px;")

        # 按钮样式随 objectName 变了要重刷
        for bn in (getattr(self, "btn_fm_borrow", None),
                   getattr(self, "btn_fm_manual", None)):
            if bn is not None:
                try:
                    bn.style().unpolish(bn)
                    bn.style().polish(bn)
                except Exception:
                    pass


    def _apply_platform_switch(self, key: str, p: dict) -> None:
        """把一个平台的开关和状态文字画对。

        ## ⚠️ 按钮写"点了会做什么"，状态写"现在是什么"

            现在是开着的 → 按钮写「关闭」 + 卡片右上角写「已开启」
            现在是关着的 → 按钮写「启用」 + 卡片右上角写「已关闭」

            原来按钮文字写的是"已开启"——那是个状态，不是动作，
            用户点下去不知道会发生什么。

        ## ⚠️ 改了要重启才生效，这一点必须让人看得见

            atrium 是长连接（飞书 websocket / 钉钉 stream），
            配置改完它不会自己重连。

            所以有三种状态要分清：
                已开启         配置开着，atrium 也在跑
                已关闭         配置关着
                **待重启**     配置改了，atrium 还没重启 —— 现在还没生效

            第三种最容易被忽略：用户点了开关，以为生效了，
            其实 atrium 里还是旧的。
        """
        sw = getattr(self, "_sw_feishu" if key == "feishu" else "_sw_dingtalk", None)
        st = getattr(self, "_ch_feishu_state" if key == "feishu" else "_ch_dingtalk_state", None)

        has_id = bool(p.get("app_id") if key == "feishu" else p.get("client_id"))
        on = p.get("enabled")
        if on is None:
            on = has_id
        on = bool(on) and has_id

        if sw is not None:
            # 按钮说"点了会做什么"
            sw.setText("关闭" if on else "启用")
            sw.setChecked(on)
            # ⚠️ 样式固定用 ghost，不要跟状态联动。
            #    黄底（switchon）在这个界面里表示"主要动作"，
            #    开关跟「测试连接」并排时两个都黄，看不出主次。
            #    开关的状态已经由按钮文字（关闭/启用）和右上角那行小字表达了。
            sw.setObjectName("ghost")
            try:
                sw.style().unpolish(sw)
                sw.style().polish(sw)
            except Exception:  # noqa: BLE001
                pass

        # ⚠️ 关着的时候，凭据输入框要**禁用**。
        #
        #    原来关掉了照样能编辑 —— 用户会以为"改了就能生效"，
        #    但 enabled=False 时 atrium 压根不读这个平台的配置，
        #    改了也没用，纯属误导。
        #
        #    （2026-11-05 老爸要求核对通道页时提的。）
        ed_id = getattr(self, "_ed_feishu_id" if key == "feishu" else "_ed_dt_id", None)
        ed_sec = getattr(self, "_ed_feishu_secret" if key == "feishu" else "_ed_dt_secret", None)
        for w in (ed_id, ed_sec):
            if w is not None:
                w.setEnabled(on)

        if st is not None:
            # 卡片右上角说"现在是什么"
            if not has_id:
                st.setText("未填凭据")
                st.setStyleSheet(f"color:{C_DIM2}; font-size:10.5px;")
                return

            # 配置跟"正在跑的 atrium"不一致 → 待重启
            #
            # 怎么知道 atrium 里是旧配置：看心跳里的 started 时间
            # 跟配置文件的修改时间。配置比 atrium 启动时间新 = 还没生效。
            pending = False
            try:
                import time as _t

                cfgp = self._atrium_config_path()
                cfg_mtime = os.path.getmtime(cfgp)
                alive = os.path.join(os.path.expanduser("~"), ".atrium", "alive.json")
                with open(alive, encoding="utf-8") as f:
                    ad = json.load(f)
                started = float(ad.get("started") or 0)
                fresh = (_t.time() - float(ad.get("ts") or 0)) < 60
                if fresh and started and cfg_mtime > started:
                    pending = True
            except Exception:  # noqa: BLE001
                pending = False

            if pending:
                st.setText("待重启")
                st.setStyleSheet(f"color:{C_ACCENT}; font-size:10.5px;")
            elif on:
                st.setText("已开启")
                st.setStyleSheet(f"color:{C_OK}; font-size:10.5px;")
            else:
                st.setText("已关闭")
                st.setStyleSheet(f"color:{C_DIM2}; font-size:10.5px;")

    def _atrium_save(self, platform: str) -> None:
        d = self._atrium_read()
        plats = d.setdefault("platforms", {})
        p = plats.setdefault(platform, {})

        # ⚠️ enabled 不能再无条件设成 True。
        #
        #    原来这里写死 p["enabled"] = True ——
        #    于是"只开飞书"根本做不到：两个都保存过一次，
        #    两个都被设成 True 了。用户点了保存，反而把不想开的那个开了。
        #
        #    现在：这个键已经存在就保留，不存在才默认 True。
        #    要关掉用开关（_atrium_toggle）。
        if "enabled" not in p:
            p["enabled"] = True

        if platform == "feishu":
            if self.ed_feishu_id.text().strip():
                p["app_id"] = self.ed_feishu_id.text().strip()
            if self.ed_feishu_secret.text().strip():
                p["app_secret"] = self.ed_feishu_secret.text().strip()
        else:
            if self.ed_dt_id.text().strip():
                p["client_id"] = self.ed_dt_id.text().strip()
            if self.ed_dt_secret.text().strip():
                p["client_secret"] = self.ed_dt_secret.text().strip()
        self._atrium_write(d)
        QMessageBox.information(
            self, "通道",
            "已保存。\n\n"
            "⚠️ 改了凭据或开关之后，要**重启 atrium** 才生效"
            "（它是长连接，不会自己重新连）。",
        )

    def _atrium_toggle(self, platform: str) -> None:
        """开 / 关一个平台 —— **当场重启 atrium 让它生效**。

        ⚠️ 老爸的原话：「开关飞书钉钉其实就是重启呀，对不对？不重启怎么生效呢？」

            对。atrium 是长连接（飞书 websocket / 钉钉 stream），
            配置改了它不会自己重连。所以"点一下开关"这件事，
            必须包含"让它重连"这一步 —— 否则就是改了个文件，
            用户以为生效了，实际上还在按旧的跑。

            原来这里只写配置 + 弹一句"要重启 atrium 才生效"，
            等于把活推给用户，他还得自己去命令行敲。

        ⚠️ 重启要十几秒，不能卡住界面。

            所以走后台线程，期间按钮变灰、文字变成「重启中…」，
            好了再恢复。主线程只负责画。
        """
        d = self._atrium_read()
        plats = d.setdefault("platforms", {})
        p = plats.setdefault(platform, {})

        cur = p.get("enabled")
        if cur is None:
            cur = True
        new = not cur
        p["enabled"] = new
        self._atrium_write(d)

        name = "飞书" if platform == "feishu" else "钉钉"
        sw = getattr(self, "_sw_feishu" if platform == "feishu" else "_sw_dingtalk", None)

        # 先把界面标成"重启中"
        if sw is not None:
            sw.setEnabled(False)
            sw.setText("重启中…")
        st = getattr(self, "_ch_feishu_state" if platform == "feishu" else "_ch_dingtalk_state", None)
        if st is not None:
            st.setText("重启中…")
            st.setStyleSheet(f"color:{C_ACCENT}; font-size:10.5px;")

        import threading

        def work() -> None:
            msg = ""
            ok = False
            try:
                from tools import atrium_proc

                ok, msg = atrium_proc.restart()
            except Exception as e:  # noqa: BLE001
                ok, msg = False, f"{type(e).__name__}: {e}"

            def apply() -> None:
                if sw is not None:
                    sw.setEnabled(True)
                # 重画状态（_apply_platform_switch 会按最新配置和心跳算）
                try:
                    self._load_atrium()
                except Exception:  # noqa: BLE001
                    pass
                # 顶栏灯也跟着刷
                try:
                    self.api.get("/api/services")
                except Exception:  # noqa: BLE001
                    pass
                if not ok:
                    QMessageBox.warning(
                        self, "通道",
                        f"{name}已{'开启' if new else '关闭'}，但 atrium 重启有问题：\n\n{msg}",
                    )

            q = getattr(self, "_main_queue", None)
            if q is not None:
                q.append(apply)
            else:
                apply()

        threading.Thread(target=work, daemon=True).start()

    def _front_mode(self, mode: str) -> None:
        """切前台的模型模式：borrow（借任务端）/ own（单独一套）。

        ## ⚠️ atrium 那边读的是这三个字段

            front.base_url
            front.api_key
            front.model

            三个都有 → 用自己这套
            否则     → 借任务端那套（engine.json 的 active）

            所以"借任务端"= 把 front 段整个删掉，让它走 fallback。
            不是写个 type:borrow —— 那个字段 atrium 不认
            （我第一版就是这么写的，等于白写）。
        """
        d = self._atrium_read()

        if mode == "borrow":
            d.pop("front", None)
            self._atrium_write(d)
            self._load_atrium()
            return

        # own：表单显示出来，让用户填
        try:
            self.front_form.setVisible(True)
        except Exception:  # noqa: BLE001
            pass

    def _save_front(self) -> None:
        """保存「单独一套」的前台模型。"""
        base = self.ed_front_base.text().strip()
        key = self.ed_front_key.text().strip()
        model = self.ed_front_model.text().strip()

        d = self._atrium_read()
        cur = d.get("front") or {}

        # 地址和模型名必须填；Key 留空 = 用已保存的那个
        if not base or not model:
            QMessageBox.warning(self, "前台模型", "接口地址和模型名都得填。")
            return
        final_key = key or str(cur.get("api_key") or "")
        if not final_key:
            QMessageBox.warning(self, "前台模型", "还没填 API Key。")
            return

        d["front"] = {
            "base_url": base,
            "api_key": final_key,
            "model": model,
        }
        self._atrium_write(d)
        self._load_atrium()
        QMessageBox.information(
            self, "前台模型",
            "已保存。\n\n⚠️ 要重启 atrium 才生效（去平台卡上点一下开关）。",
        )

    def _atrium_save_engine(self) -> None:
        """保存「atrium 连到任务端」这张卡。

        ⚠️ 原来只存了 engine.path，队列落盘目录（ed_queue_dir）
            有输入框但保存逻辑里根本没读它。

            实测（2026-11-05 老爸要求核对通道页时发现）。
        """
        d = self._atrium_read()

        # ── engine 段 ──
        eng = d.setdefault("engine", {})
        eng["type"] = "remote-dispatch"
        eng["path"] = self.ed_engine_path.text().strip() or eng.get("path", "")

        # ── storage 段（队列落盘目录）──
        #
        # atrium 那边读的是 storage.dir，留空就用自己的默认位置。
        qdir = self.ed_queue_dir.text().strip()
        if qdir:
            d.setdefault("storage", {})["dir"] = qdir
        else:
            # 留空 = 用默认，把键删掉（别存一个空字符串）
            st = d.get("storage")
            if isinstance(st, dict):
                st.pop("dir", None)

        self._atrium_write(d)
        QMessageBox.information(
            self, "通道",
            "已保存。\n\n"
            "⚠️ 改了位置之后，要**重启 atrium** 才生效 —— "
            "去平台卡上点一下那个开关（关再开）就行。",
        )
        """保存「atrium 连到任务端」和「前台用哪套模型」两卡。

        ⚠️ 原来只存了 engine.path，下面两个东西都没存：
            · 队列落盘目录（ed_queue_dir）—— 有输入框，但保存逻辑里没读它
            · 前台模型（原来那两个按钮压根没接信号）

            实测（2026-11-05 老爸要求核对通道页时发现）。
        """
        d = self._atrium_read()

        # ── engine 段 ──
        eng = d.setdefault("engine", {})
        eng["type"] = "remote-dispatch"
        eng["path"] = self.ed_engine_path.text().strip() or eng.get("path", "")

        # ── storage 段（队列落盘目录）──
        #
        # atrium 那边读的是 storage.dir，留空就用自己的默认位置。
        qdir = self.ed_queue_dir.text().strip()
        if qdir:
            d.setdefault("storage", {})["dir"] = qdir
        else:
            # 留空 = 用默认，把键删掉（别存一个空字符串）
            st = d.get("storage")
            if isinstance(st, dict):
                st.pop("dir", None)

        # ── front 段不在这里管 ──
        #
        # atrium 读的是 front.base_url / front.api_key / front.model，
        # 三个都有才用自己那套（见 atrium/core/app.py:139）。
        #
        # 前台模型的编辑在上面的卡里（_front_mode / _save_front 负责），
        # 这里不碰 —— 原来这里写 {"type":"borrow"} 是错的，
        # atrium 不认 type/profile 这两个字段，等于白写。

        self._atrium_write(d)
        QMessageBox.information(
            self, "通道",
            "已保存。\n\n"
            "⚠️ 改了位置之后，要**重启 atrium** 才生效 —— "
            "去平台卡上点一下那个开关（关再开）就行。",
        )

    def _atrium_test_feishu(self) -> None:
        QMessageBox.information(self, "测试连接", "先保存，再到 atrium 目录跑 `python -m atrium.cli test`。")

    def _atrium_test_dingtalk(self) -> None:
        QMessageBox.information(self, "测试连接", "先保存，再到 atrium 目录跑 `python -m atrium.cli test`。")


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

    # 启动器（start_desktop.pyw）已经把后台拉起来了：这里只等它，别再起一个。
    # 两个进程同时抢同一个端口，结果是其中一个 bind 失败、日志还难看。
    if os.environ.get("DSH_BRIDGE_BACKEND_SPAWNED") == "1":
        for _ in range(75):          # 最多 30 秒
            time.sleep(0.4)
            if _port_open(_API_PORT):
                return True
        # 等不到说明那边没起来，清了标记自己来 —— 别一直卡在"启动中"
        os.environ.pop("DSH_BRIDGE_BACKEND_SPAWNED", None)

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


# ────────────────────────────────────────────────────────────────────
# 单实例
# ────────────────────────────────────────────────────────────────────
_LOCAL_NAME = "dsh-bridge-desktop"
_INSTANCE = {"server": None}
_MAIN_WIN = None


def _raise_window() -> None:
    w = _MAIN_WIN
    if w is None:
        return
    w.showNormal()
    w.raise_()
    w.activateWindow()


def _claim_single_instance() -> bool:
    """自己是第一个实例就返回 True；已有实例就把它叫到前面，返回 False。

    ⚠️ 为什么要有这个：
        双击图标没有立刻出窗口时，人的第一反应是再双击一次。
        原来那样会开出两个窗口、两份定时器，
        两个都在轮询，看着像程序在抽搐。

    ⚠️ 为什么用 QLockFile 而不是"连一下管道"：
        第一版实现是"能连上管道就说明已经有实例"。
        实测本机同时双击（间隔不到 1 秒）时**两个窗口都开出来了**：
        第一个进程还没 listen 完，第二个就去连，连不上；
        然后两个都 removeServer + listen —— Windows 的命名管道
        允许同名多实例，于是两个都 listen 成功。

        QLockFile 是文件级独占锁，带 PID 和僵死检测，
        两个进程同时 tryLock 只可能有一个成功。
        管道留着，只用来把已有窗口叫到前面。
    """
    lock = QLockFile(_userdirs.config_file("desktop.lock"))
    lock.setStaleLockTime(20000)     # 崩掉的进程留下的锁，20 秒后算过期
    if not lock.tryLock(100):
        _poke_existing()
        return False
    _INSTANCE["lock"] = lock

    from PySide6.QtNetwork import QLocalServer

    # 上次崩掉可能留下一个连不上的 server，先清掉再监听
    QLocalServer.removeServer(_LOCAL_NAME)
    srv = QLocalServer()
    if srv.listen(_LOCAL_NAME):
        def on_new_connection() -> None:
            while srv.hasPendingConnections():
                c = srv.nextPendingConnection()
                if c is not None:
                    c.readAll()
                    c.disconnectFromServer()
            _raise_window()

        srv.newConnection.connect(on_new_connection)
        _INSTANCE["server"] = srv

    return True


def _poke_existing() -> None:
    """通知已经开着的那个窗口：到前台来。失败就当没这回事。"""
    try:
        from PySide6.QtNetwork import QLocalSocket

        sock = QLocalSocket()
        sock.connectToServer(_LOCAL_NAME)
        if sock.waitForConnected(300):
            sock.write(b"raise")
            sock.waitForBytesWritten(300)
            sock.disconnectFromServer()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    # ⚠️ 顺序很重要：先建 QApplication、先开窗口。
    #
    #    原来第一句是 _spawn_backend() —— 冷启动时它要起进程、等端口，
    #    最长 24 秒。这段时间里窗口根本没画出来，用户看到的就是
    #    "双击了，没反应"。
    #
    #    现在：窗口先出现在屏幕上（后台那条提示条说明进度），
    #    后台在后台线程里起，好了提示条自己消失。
    app = QApplication(sys.argv)
    app.setApplicationName("dsh-bridge")
    app.setOrganizationName("dsh-bridge")

    if not _claim_single_instance():
        # 已经有窗口了，它会被叫到前面；这个进程直接退
        return 0

    global _MAIN_WIN
    w = MainWindow()
    _MAIN_WIN = w
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
