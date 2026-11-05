"""界面里那些自己画的小控件。

## 为什么单独拆出来

    原来是 desktop/app.py 的一部分（那个文件最大时 4311 行）。
    这些控件跟"哪一页"无关 —— 任务页、服务页、设置页都可能用到。

    拆出来之后：
        · 改一个控件的画法只开这个文件
        · 能单独测（画出来看快照，不用起整个窗口）
        · 各页 import 这里，不关心内部怎么画的

## 依赖

    theme（颜色、字体）
    没有别的项目内部依赖 —— **不能 import app.py**，否则循环。

## 用到 Qt 的哪些东西

    这些都是自绘 / 轻量封装：
        GlowDot          一个带呼吸感的状态点
        StatusBadge      状态胶囊（完成 / 运行中 / 失败...）
        GradientBar      顶部那条渐变线
        TaskRow          任务列表里的一行
        BusyIndicator    转圈
        PathRow          设置页里"路径 + 选择按钮"那一行
        ServiceCard      服务页的服务卡片
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from theme import (
    C_ACCENT, C_ACCENT2, C_ACCENT3, C_BAD, C_BG, C_DIM, C_DIM2,
    C_GRAD_A, C_GRAD_B, C_INPUT, C_LINE, C_LINE_HI, C_OK, C_RAISED,
    C_RUN, C_SURFACE, C_TEXT, C_TEXT2, C_WARN,
    STATUS_TEXT, _text_width, font, money, secs, status_color, toks,
)


class GlowDot(QWidget):
    """带光晕的状态点。"""

    def __init__(self, color: str = C_DIM2, size: int = 8):
        super().__init__()
        self._color = color
        self._size = size
        self.setFixedSize(size + 8, size + 8)

    def set_color(self, color: str) -> None:
        # 颜色没变就不重绘。轮询很频繁，这个判断省掉大量无谓的 update()
        if self._color == color:
            return
        self._color = color
        self.update()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        r = self._size / 2

        glow = QColor(self._color)
        glow.setAlpha(55)
        p.setBrush(glow)
        p.setPen(Qt.NoPen)
        p.drawEllipse(int(cx - r - 3), int(cy - r - 3), self._size + 6, self._size + 6)

        p.setBrush(QColor(self._color))
        p.drawEllipse(int(cx - r), int(cy - r), self._size, self._size)



class StatusBadge(QLabel):
    """圆角胶囊状态徽章。"""

    def __init__(self, status: str):
        super().__init__(STATUS_TEXT.get(status, status))
        c = status_color(status)
        bg = QColor(c)
        bg.setAlpha(20)
        border = QColor(c)
        border.setAlpha(70)
        self.setStyleSheet(
            f"color:{c}; background:{bg.name(QColor.HexArgb)};"
            f"border:1px solid {border.name(QColor.HexArgb)};"
            "border-radius:9px; padding:1px 9px; font-size:11px;"
        )
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)



class GradientBar(QWidget):
    """顶栏下方一条细渐变线，让界面不那么平。"""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(2)

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        g = QLinearGradient(0, 0, self.width(), 0)
        c1 = QColor(C_GRAD_A)
        c2 = QColor(C_GRAD_B)
        c1.setAlpha(200)
        c2.setAlpha(90)
        g.setColorAt(0.0, c1)
        g.setColorAt(0.45, c2)
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), g)



class TaskRow(QFrame):
    """任务列表的一行。

    设计上要同时满足两件事：
        1. 看着清楚：状态色条 + 名字 + 摘要 + 时间花费
        2. 刷新便宜：所有子控件都存成属性，能原地改文字，
           而不是每次销毁重建

    第 2 点不是过度优化。实测 100 行重建要 435 毫秒，
    列表三秒刷一次，界面就有一大半时间在重建控件——
    屏幕上持续闪烁，点什么都没反应。
    """

    clicked = Signal(str)

    @staticmethod
    def _badge_style(status: str) -> str:
        c = status_color(status)
        bg = QColor(c)
        bg.setAlpha(20)
        border = QColor(c)
        border.setAlpha(70)
        return (
            f"color:{c}; background:{bg.name(QColor.HexArgb)};"
            f"border:1px solid {border.name(QColor.HexArgb)};"
            "border-radius:9px; padding:1px 9px; font-size:11px;"
        )

    def __init__(self, t: dict):
        super().__init__()
        self.name = t.get("name", "")
        self.setObjectName("taskrow")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(60)

        st = t.get("status", "")
        color = status_color(st)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 16, 0)
        root.setSpacing(0)

        # 左侧状态色条
        bar_wrap = QVBoxLayout()
        bar_wrap.setContentsMargins(8, 12, 0, 12)
        self._bar = QFrame()
        self._bar.setFixedWidth(3)
        self._bar.setStyleSheet(f"background:{color}; border-radius:1px;")
        bar_wrap.addWidget(self._bar)
        root.addLayout(bar_wrap)

        body = QVBoxLayout()
        body.setContentsMargins(12, 10, 0, 10)
        body.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)

        self._name = QLabel(self.name)
        self._name.setFont(font(13, QFont.DemiBold))
        self._name.setStyleSheet(f"color:{C_TEXT};")
        top.addWidget(self._name)

        top.addStretch()
        body.addLayout(top)

        self._desc = QLabel((t.get("task") or "").replace("\n", " "))
        self._desc.setFont(font(12))
        self._desc.setStyleSheet(f"color:{C_DIM};")
        self._desc.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._desc.setToolTip((t.get("task") or "")[:600])
        body.addWidget(self._desc)

        root.addLayout(body, 1)

        right = QVBoxLayout()
        right.setContentsMargins(12, 12, 0, 12)
        right.setSpacing(4)
        right.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._time = QLabel(secs(t.get("seconds")))
        self._time.setFont(font(11))
        # ⚠️ 颜色跟左边那条状态色条**保持一致**。
        #
        #    原来固定写死成 C_DIM2（灰），于是：
        #        左边色条是绿的（完成）/ 红的（失败）
        #        右边那个时间永远是灰的
        #    一眼扫下去分不清哪条成、哪条败 —— 还得回头找最左边那条细线。
        #
        #    实测（2026-11-05）：老爸指着那一列说
        #    「后面的数字和 — 颜色应该跟竖条一样才对，这样就好分辨」。
        self._time.setStyleSheet(f"color:{color};")
        self._time.setAlignment(Qt.AlignRight)
        right.addWidget(self._time)

        cost_v = t.get("cost_cny")
        hot = isinstance(cost_v, (int, float)) and cost_v >= 0.5
        self._cost = QLabel(money(cost_v))
        self._cost.setFont(font(11))
        # 花钱多的用橙色提醒（那是另一回事），其余跟状态同色
        self._cost.setStyleSheet(
            f"color:{C_WARN if hot else color}; font-size:11px;"
        )
        self._cost.setAlignment(Qt.AlignRight)
        right.addWidget(self._cost)

        root.addLayout(right)

    def update_data(self, t: dict) -> None:
        """原地更新内容，不重建控件。

        列表每几秒刷一次，两次之间绝大多数行根本没变。
        只改真正变了的文字，开销接近零。
        """
        st = t.get("status", "")
        color = status_color(st)

        self.name = t.get("name", "")
        self._bar.setStyleSheet(f"background:{color}; border-radius:1px;")

        self._name.setText(self.name)

        desc = (t.get("task") or "").replace("\n", " ")
        if self._desc.text() != desc:
            self._desc.setText(desc)
            self._desc.setToolTip((t.get("task") or "")[:600])

        # ⚠️ 时间/费用的颜色要**跟着状态走**，刷新时也得重设。
        #
        #    原来这里只在文字变了的时候才动，而且颜色写死 C_DIM2。
        #    状态从"在跑"变"完成"，色条变绿了，右边数字还是灰的。
        #
        #    现在：每次刷新都把颜色对一遍（setStyleSheet 很便宜，
        #    而且这里本来就每次都在跑）。
        tm = secs(t.get("seconds"))
        if self._time.text() != tm:
            self._time.setText(tm)
        self._time.setStyleSheet(f"color:{color};")

        cost_v = t.get("cost_cny")
        cm = money(cost_v)
        if self._cost.text() != cm:
            self._cost.setText(cm)
        hot = isinstance(cost_v, (int, float)) and cost_v >= 0.5
        self._cost.setStyleSheet(
            f"color:{C_WARN if hot else color}; font-size:11px;"
        )

    def mousePressEvent(self, e):  # noqa: N802
        self.clicked.emit(self.name)
        super().mousePressEvent(e)



class BusyIndicator(QWidget):
    """一个转圈的小弧，表示"我在动"。

    为什么要这个：
        用户点完按钮如果只是文字变一下、然后干等，
        他分不清"正在启动"还是"卡死了"。转圈是通用的"别急"信号。

    ⚠️ 定时器不能常驻：
        原来 start(60) 写在构造函数里，每个卡片一创建就挂一个
        60ms 的定时器，谁也停不了。卡片重建时越堆越多，
        几十个定时器一起调 update()，整个窗口就在闪。
        改成跟着可见性开关。
    """

    def __init__(self, size: int = 16):
        super().__init__()
        self._angle = 0
        self._size = size
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        if not self._timer.isActive():
            self._timer.start(60)

    def hideEvent(self, e):  # noqa: N802
        self._timer.stop()
        super().hideEvent(e)

    def _tick(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, _):  # noqa: N802
        from PySide6.QtGui import QPen

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._size / 2 - 2
        pen = QPen(QColor(C_ACCENT))
        pen.setWidth(2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.translate(self._size / 2, self._size / 2)
        p.rotate(self._angle)
        p.drawArc(int(-r), int(-r), int(r * 2), int(r * 2), 0, 270 * 16)



class PathRow(QWidget):
    """一行路径设置：标签 + 输入框 + 浏览按钮。

    让人手打一条 Windows 路径是在为难人，特别是 AppData 下面
    那种长路径，错一个字符就找不到。点"选择"用系统对话框选。
    """

    changed = Signal()

    def __init__(self, key: str, label: str, hint: str = "",
                 pick_dir: bool = False, pick_name: str = ""):
        super().__init__()
        self.key = key
        self.pick_dir = pick_dir
        self.pick_name = pick_name

        # 高度必须钉死：放进 QVBoxLayout 又没有弹簧时，
        # Qt 会把控件压到最小高度，标签挤成一条线。
        # 高度要跟着输入框一起变：16(标签) + 38(输入框) + 3(间距) + 余量
        self.setFixedHeight(62)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        top = QLabel(label)
        top.setFixedHeight(16)
        top.setFont(font(11))
        top.setStyleSheet(f"color:{C_DIM2};")
        lay.addWidget(top)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.edit = QLineEdit()
        self.edit.setMinimumHeight(36)
        self.edit.setPlaceholderText(hint or "自动探测不到，点右边选择")
        # textChanged 带一个字符串参数，changed 是无参信号，
        # 直接连会报错，用 lambda 吞掉。
        self.edit.textChanged.connect(lambda _t: self.changed.emit())
        row.addWidget(self.edit, 1)

        btn = QPushButton("选择…")
        btn.setObjectName("ghost")
        btn.setMinimumHeight(36)
        btn.setMinimumWidth(70)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self._browse)
        row.addWidget(btn)
        lay.addLayout(row)

    def value(self) -> str:
        return self.edit.text().strip()

    def set_value(self, v: str) -> None:
        self.edit.setText(v or "")

    def _browse(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        if self.pick_dir:
            path = QFileDialog.getExistingDirectory(self, "选择目录", self.value() or "")
        else:
            filt = (f"{self.pick_name or '程序'} (*.exe);;所有文件 (*.*)"
                    if self.pick_name else "")
            path, _ = QFileDialog.getOpenFileName(
                self, "选择文件", self.value() or "", filt)
        if path:
            self.set_value(path)



class ServiceCard(QFrame):
    """服务页的一张卡。

    按钮有两个状态，跟着服务真实状态走：
        没起来 → 「启动」
        起来了 → 「重启」

    ⚠️ 这里返工过一次：
        原来有两个同名的 set_working 方法叠着，后一个把前一个覆盖了，
        所以不管成功失败，按钮永远停在"重启中…"。
        Python 不会报错，两个方法默默后者胜——这类错误只能靠跑出来发现。
        现在合并成一个 mark_working / clear_working，职责分开。
    """

    action_clicked = Signal(str, str)

    def __init__(self, key: str, title: str, desc: str):
        super().__init__()
        self.key = key
        self.setObjectName("card")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(14)

        self.dot = GlowDot(C_DIM2, 9)
        lay.addWidget(self.dot, 0, Qt.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(3)
        nm = QLabel(title)
        nm.setFont(font(13, QFont.DemiBold))
        nm.setStyleSheet(f"color:{C_TEXT};")
        ds = QLabel(desc)
        ds.setFont(font(11.5))
        ds.setStyleSheet(f"color:{C_DIM};")
        text.addWidget(nm)
        text.addWidget(ds)
        lay.addLayout(text, 1)

        self.btn = QPushButton("启动")
        self.btn.setObjectName("ghost")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.clicked.connect(self._on_click)
        lay.addWidget(self.btn)

        # 转圈动画：告诉用户"正在处理，别急"。
        # 光换个文字不够——用户会怀疑是不是卡住了。
        self._spin = BusyIndicator()
        self._spin.hide()
        lay.addWidget(self._spin)

        self._running = False
        self._working = False

    def _on_click(self) -> None:
        if self._working:
            return
        self.action_clicked.emit(self.key, "restart" if self._running else "start")

    def _refresh(self) -> None:
        """按当前状态更新按钮外观。

        ⚠️ 只在真的变了的时候才动控件。
            原来是每次都 setText + unpolish + polish，
            polish 会强制整个控件重新计算样式并重绘。
            服务状态是每 1.5 秒轮询一次的，于是按钮每 1.5 秒
            就闪一下——看起来就是"桌面端在自己乱闪"。

            现在记住上一次的目标状态，一样就直接返回。
        """
        want_working = self._working
        want_text = ("启动中…" if want_working
                     else ("重启" if self._running else "启动"))
        want_obj = "working" if want_working else "ghost"

        if (getattr(self, "_last_text", None) == want_text
                and getattr(self, "_last_obj", None) == want_obj
                and getattr(self, "_last_enabled", None) == (not want_working)):
            return

        self._last_text = want_text
        self._last_obj = want_obj
        self._last_enabled = not want_working

        self.btn.setText(want_text)
        self.btn.setEnabled(not want_working)
        self.btn.setObjectName(want_obj)
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)

        if want_working:
            self._spin.show()
        else:
            self._spin.hide()

    def set_ok(self, ok: bool) -> None:
        """服务真实状态变了。

        状态一回来就把 working 清掉——不依赖任何请求的结果。
        这就是"点了我先不管还是卡在重启中"那个问题的解药。

        没变化时什么也不做：轮询每 1.5 秒来一次，
        每次都重绘一遍就是闪。
        """
        ok = bool(ok)
        if self._running == ok and not self._working:
            return
        self._running = ok
        self.dot.set_color(C_OK if ok else C_BAD)
        self._working = False
        self._refresh()

    def mark_working(self) -> None:
        self._working = True
        self.dot.set_color(C_WARN)
        self._refresh()

    def clear_working(self) -> None:
        self._working = False
        self._refresh()


# ────────────────────────────────────────────────────────────────────
# 任务详情
# ────────────────────────────────────────────────────────────────────


