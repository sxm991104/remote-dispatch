"""任务详情弹窗。

## 为什么单独拆出来

    原来它在 desktop/app.py 里。

    拆 mixin 的时候发现：
        tasks_mixin 里要弹它（_open_task）
        但它定义在 app.py 里

    而 app.py 要 import tasks_mixin —— 反过来 import 就循环了。

    所以它必须搬到 mixin 能 import 的、不会反向依赖的地方。
    dialogs.py 就是这个地方：它依赖 api / theme / widgets，**不依赖 app.py**。

## 依赖

    api（拿详情）
    theme（颜色、字体）
    widgets（状态胶囊之类）

    ⚠️ 不能 import app.py，否则循环。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from api import Api
from theme import (
    C_ACCENT, C_ACCENT2, C_ACCENT3, C_BAD, C_BG, C_DIM, C_DIM2,
    C_GRAD_A, C_GRAD_B, C_INPUT, C_LINE, C_LINE_HI, C_OK, C_RAISED,
    C_RUN, C_SURFACE, C_TEXT, C_TEXT2, C_WARN,
    STATUS_TEXT, _text_width, font, money, secs, status_color, toks,
)
from widgets import StatusBadge


class DetailDialog(QDialog):
    def __init__(self, task: dict, api: Api, parent=None):
        super().__init__(parent)
        self.api = api
        self.task = task
        self.setWindowTitle(task.get("name", "任务"))
        self.resize(780, 640)

        st = task.get("status", "")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 24, 26, 22)
        lay.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel(task.get("name", ""))
        title.setFont(font(17, QFont.Bold))
        title.setStyleSheet(f"color:{C_TEXT};")
        head.addWidget(title)
        head.addWidget(StatusBadge(st))
        head.addStretch()
        lay.addLayout(head)

        metrics = QHBoxLayout()
        metrics.setSpacing(24)
        for k, v in (
            ("耗时", secs(task.get("seconds"))),
            ("上下文", toks(task.get("context_tokens"))),
            ("每轮", money(task.get("cost_cny")) or "—"),
        ):
            col = QVBoxLayout()
            col.setSpacing(2)
            kl = QLabel(k)
            kl.setFont(font(10.5))
            kl.setStyleSheet(f"color:{C_DIM2};")
            vl = QLabel(v)
            vl.setFont(font(14, QFont.DemiBold))
            vl.setStyleSheet(f"color:{C_TEXT2};")
            col.addWidget(kl)
            col.addWidget(vl)
            metrics.addLayout(col)
        metrics.addStretch()
        lay.addLayout(metrics)

        lay.addWidget(self._label("任务内容"))
        box = QTextEdit()
        box.setPlainText(task.get("task") or "")
        box.setReadOnly(True)
        box.setFixedHeight(130)
        lay.addWidget(box)

        is_err = bool(task.get("error"))
        lay.addWidget(self._label("失败原因" if is_err else "结果"))
        out = QTextEdit()
        body = task.get("error") or task.get("result") or ""
        out.setPlainText(body or ("还在跑，等它结束" if st == "running" else "没有结果"))
        out.setReadOnly(True)
        if is_err:
            out.setStyleSheet(
                f"QTextEdit {{ background:{C_INPUT}; border:1px solid #5a3a3a;"
                f"color:{C_BAD}; border-radius:10px; padding:14px; font-size:13px; }}"
            )
        lay.addWidget(out, 1)

        btns = QHBoxLayout()
        btns.addStretch()
        if st == "running":
            b = QPushButton("停掉")
            b.setObjectName("ghost")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(self._cancel)
            btns.addWidget(b)
        if st in ("ok", "timeout", "error", "empty"):
            b = QPushButton("在同一个窗口继续")
            b.setObjectName("primary")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(self._continue)
            btns.addWidget(b)
        close = QPushButton("关闭")
        close.setObjectName("ghost")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        lay.addLayout(btns)

    def _label(self, text: str) -> QLabel:
        lb = QLabel(text)
        lb.setFont(font(11))
        lb.setStyleSheet(f"color:{C_DIM2};")
        return lb

    def _cancel(self) -> None:
        if QMessageBox.question(self, "确认", f"停掉「{self.task['name']}」？") != QMessageBox.Yes:
            return
        self.api.post("/api/cancel", {"name": self.task["name"]})
        self.accept()

    def _continue(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        msg, ok = QInputDialog.getText(self, "继续", "接着说一句：", text="继续")
        if ok and msg.strip():
            self.api.post("/api/continue", {"name": self.task["name"], "message": msg.strip()})
            self.accept()


# ────────────────────────────────────────────────────────────────────
# 主窗口
# ────────────────────────────────────────────────────────────────────

