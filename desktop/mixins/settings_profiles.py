"""设置 · 模型配置的**列表**部分。

## 为什么单独拆出来

    settings_model.py 原来 1020 行，里面是两件事：

        一、模型卡的 UI + 发请求（_card_model / _fetch_models / _save_engine）
        二、配置列表怎么画、怎么增删切换（_render_profiles 那 200 行 + 一堆操作）

    这两件事改的频率和理由完全不同：
        界面改样式      → 动一
        列表加功能      → 动二

    拆开之后各管各的。

⚠️ 从 settings_model.py 搬出来的，只搬方法，逻辑没改。
"""

from __future__ import annotations

from api import _API_PORT

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
    GlowDot,
)

from api import API
from theme import (
    C_ACCENT, C_ACCENT2, C_ACCENT3, C_BAD, C_BG, C_DIM, C_DIM2,
    C_GRAD_A, C_GRAD_B, C_INPUT, C_LINE, C_LINE_HI, C_OK, C_RAISED,
    C_RUN, C_SURFACE, C_TEXT, C_TEXT2, C_WARN,
    STATUS_TEXT, _text_width, font, money, secs, status_color, toks,
)


class SettingsProfilesMixin:

    def _load_profiles(self, attempt: int = 0) -> None:
        """拉配置列表，渲染出来。

        ⚠️ 失败不能就这样算了。
            实测：桌面端刚打开时，如果接口服务还没起来（或者刚好在重启），
            这个请求会失败。原来的代码失败就 return，
            界面上永远显示"还没有配置"——用户以为配置丢了。

            现在：失败就重试（最多 4 次，间隔递增），
            全失败了才显示一句明确的错误，而不是假装空。
        """
        def on_fail(msg: str):
            if attempt < 3:
                # 接口可能还在启动，等一下再试
                QTimer.singleShot(
                    800 * (attempt + 1),
                    lambda: self._load_profiles(attempt + 1),
                )
                if attempt == 0:
                    self.lbl_engine.setText("正在连后台…")
            else:
                self.lbl_engine.setText(f"读不到配置：{msg}")
                # 显示一个明确的状态，别让用户以为配置丢了
                self._profile_error = msg
                self._render_profiles()

        # 每次请求带个序号，只认最后一次的结果。
        # 切页快了的话会有多个请求在飞，老的晚到会把新的覆盖掉——
        # 表现是"刚选的那套又跳回去了"。
        self._prof_seq = getattr(self, "_prof_seq", 0) + 1
        my_seq = self._prof_seq

        def done(data):
            if my_seq != getattr(self, "_prof_seq", 0):
                return  # 已经有更新的请求了，这个结果不要
            if not isinstance(data, dict) or not data.get("ok"):
                on_fail((data or {}).get("error") or "接口没回应")
                return
            self._profile_error = ""
            self._profiles = data.get("profiles") or []
            self._active_pid = data.get("active") or ""
            self._render_profiles()

            # ⚠️ 只在**第一次**才把某一套填进编辑区。
            #
            #    原来这里有段"永远填当前那套"的逻辑，
            #    而整个列表每 3 秒刷新一次——
            #    结果是用户点哪套，两秒后就被拽回去，
            #    根本没法改另一套。
            #
            #    现在：用户点过就听用户的，不再自动改。
            #    用户删掉选中的那套时，才重新挑一个。
            # ⚠️ 正在新增的时候，**什么都别填**。
            #
            #    用户点了「＋ 新增模型」，界面清空成空白表单，
            #    _selected_pid 也清成 ""。
            #
            #    而这一段是每 3 秒跑一次的刷新逻辑：
            #        发现"选中的那套"不存在 → 以为被删了 → 挑第一套填进去
            #    于是用户填到一半的空白表单，两秒后被第一套覆盖了。
            #
            #    现在：新增模式优先，刷新不碰编辑区。
            if getattr(self, "_is_adding", False):
                return

            sel = getattr(self, "_selected_pid", "")
            still = next(
                (x for x in self._profiles if x["id"] == sel), None
            )
            if still is None:
                # 没选过，或者选中的那套被删了
                pick = self._profiles[0] if self._profiles else None
                if pick:
                    self._selected_pid = pick["id"]
                    self._fill_editor(pick)

        self.api.request_once("/api/engine/profiles", callback=done)

    def _render_profiles(self) -> None:
        """把配置列表画出来。

        每行三个信息：是不是当前在用的（绿点）、叫什么、用哪个模型。
        不在用的那套给一个「启用」小按钮；在用的只显示状态，不给按钮。

        样式上特意做了三件事：
            · 每行高度统一，不因为有没有警告标签而参差
            · 选中的那行用左侧竖条标识，不用整行灰底（灰底看着像禁用）
            · 「缺 Key」放到行尾固定位置，不挤着模型名
        """
        while self.profile_box.count():
            it = self.profile_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        if not getattr(self, "_profiles", None):
            err = getattr(self, "_profile_error", "")

            # ⚠️ 这句提示原来写的是「后台可能还没起来，重开一下这个窗口」。
            #
            #    实测：按 README 跑 python -m desktop.app 的人，
            #    界面开了但接口服务没人起（那是 start_desktop.pyw 干的）。
            #    于是这句提示一直挂着，用户就一直重开窗口 ——
            #    **重开一百次也一样**，因为缺的不是窗口，是后台进程。
            #
            #    所以分成三种情况说清楚：
            #      连不上   → 告诉他们接口服务没起，给出两条起法
            #      token 错 → 那是另一个问题（通常是后台残留旧 token）
            #      其他     → 原样显示，别瞎猜
            _msg = ""
            if err:
                low = err.lower()
                if "token" in low:
                    _msg = (
                        f"后台认不出这个窗口的令牌：{err}\n"
                        "通常是后台进程是旧的（换了 token 但没重启）。"
                        "把桌面端关掉重开一次；还不行就在任务管理器里"
                        "结束 python 进程再打开。"
                    )
                elif "timed out" in low or "拒绝" in err or "refused" in low \
                        or "urlopen" in low or "connect" in low:
                    _msg = (
                        f"连不上后台（127.0.0.1:{_API_PORT}）。\n"
                        "界面和后台是两个进程，刚才那个命令只开了界面。\n"
                        "两种起法：\n"
                        "  一、双击 start_desktop.pyw（会先把后台拉起来）\n"
                        "  二、另开一个窗口跑 python -m server.api_server"
                    )
                else:
                    _msg = f"读不到配置：{err}"
            else:
                _msg = "还没有模型。点下面「＋ 新增模型」。"

            lb = QLabel(_msg)
            lb.setFont(font(11))
            lb.setWordWrap(True)
            lb.setStyleSheet(
                f"color:{C_WARN if err else C_DIM2}; padding:8px 0;"
            )
            self.profile_box.addWidget(lb)
            return

        for prof in self._profiles:
            # ⚠️ 没有"当前"这个概念了。
            #
            #    每套配置只有一个状态：开 / 关。
            #    开着的都可以同时跑（各自限流、各自排队）。
            #    用户选哪一套只是"选中来编辑"，不影响跑不跑。
            is_sel = prof["id"] == getattr(self, "_selected_pid", "")

            row = QFrame()
            row.setObjectName("profrow")
            row.setCursor(Qt.PointingHandCursor)
            row.setFixedHeight(44)
            row.setStyleSheet(
                "QFrame#profrow {"
                f" background:{C_INPUT};"
                f" border:1px solid {C_LINE_HI if is_sel else C_LINE};"
                f" border-left:3px solid {C_ACCENT if is_sel else 'transparent'};"
                " border-radius:8px;"
                "}"
            )

            rl = QHBoxLayout(row)
            rl.setContentsMargins(12, 0, 12, 0)
            rl.setSpacing(10)

            # 状态点：只有两种
            #   开着  绿（参与派活）
            #   关着  灰（不参与）
            is_on = prof.get("enabled", True)
            dot = GlowDot(C_OK if is_on else C_DIM2, 7)
            rl.addWidget(dot, 0, Qt.AlignVCenter)

            # 名字（固定宽度，让后面的对齐）
            nm = QLabel(prof.get("name") or "未命名")
            nm.setFont(font(12, QFont.DemiBold))
            nm.setStyleSheet(f"color:{C_TEXT};")
            nm.setFixedWidth(110)
            rl.addWidget(nm)

            # 模型名
            md = QLabel(prof.get("model") or "(没填模型)")
            md.setFont(font(11))
            md.setStyleSheet(
                f"color:{C_DIM if prof.get('model') else C_DIM2};"
            )
            rl.addWidget(md, 1)

            # 缺 Key 提示：固定位置，不挤着别的
            if not prof.get("has_key"):
                warn = QLabel("缺 Key")
                warn.setFont(font(10))
                warn.setStyleSheet(f"color:{C_WARN};")
                warn.setFixedWidth(48)
                warn.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                rl.addWidget(warn)

            # 右侧：**每套独立开关** + 删除
            #
            # ⚠️ 这里改过一次，原来只有两种状态：
            #        在用      显示"✓ 正在用"
            #        没在用    [启用] ← 点了会切过去
            #
            #    但那不是"开关"，是"切换"。用户想的是：
            #        这套先用着，那套先关掉
            #    而不是"只能有一个在用，选另一个就换掉"。
            #
            #    现在每套都有自己的开关：
            #        开着  这套参与派活（可以是多套同时开，各跑各的）
            #        关着  不参与，队列里也不排它
            #
            #    另外"当前默认用哪套"是另一件事，点行本身来切。

            # 开关
            #
            # ⚠️ 按钮文字说的是"点下去会怎样"，不是"现在什么状态"。
            #
            #    原来写的是「开着 / 关着」——那是描述状态，
            #    用户看见「开着」会以为"点它也是开"，不敢点。
            #
            #    现在：开着 →「停用」，关着 →「启用」。
            #    状态本身由左边的绿点/灰点表示。
            sw = QPushButton("停用" if is_on else "启用")
            sw.setCursor(Qt.PointingHandCursor)
            sw.setFixedSize(52, 26)
            # ⚠️ 颜色跟着**动作**走，不跟状态走。
            #
            #    原来：开着绿、关着灰——颜色在描述状态，
            #    文字却说"停用/启用"，两边打架。
            #
            #    现在：
            #        开着 →「停用」红描边（点了会关掉，给个警示色）
            #        关着 →「启用」绿实心（点了会打开，给个正色）
            #
            #    这样看颜色就知道点下去是"好事"还是"会关掉"。
            if is_on:
                sw.setStyleSheet(
                    f"QPushButton{{color:{C_BAD}; background:transparent;"
                    f"border:1px solid {C_BAD}; border-radius:6px;"
                    f"font-size:11px;}}"
                    f"QPushButton:hover{{background:{C_BAD}; color:white;}}"
                )
            else:
                sw.setStyleSheet(
                    f"QPushButton{{color:white; background:{C_OK};"
                    f"border:1px solid {C_OK}; border-radius:6px;"
                    f"font-size:11px;}}"
                    f"QPushButton:hover{{background:{C_OK}; color:white;}}"
                )
            sw.clicked.connect(
                lambda _=False, pid=prof["id"], now=is_on: self._toggle_profile(pid, now)
            )
            rl.addWidget(sw)

            # 删除
            dele = QPushButton("删")
            dele.setCursor(Qt.PointingHandCursor)
            dele.setFixedSize(30, 26)
            dele.setStyleSheet(
                f"QPushButton{{color:{C_DIM2}; background:transparent;"
                f"border:1px solid {C_LINE}; border-radius:6px;"
                f"font-size:11px;}}"
                f"QPushButton:hover{{color:{C_BAD}; border-color:{C_BAD};}}"
            )
            dele.clicked.connect(
                lambda _=False, pid=prof["id"]: self._delete_profile_by_id(pid)
            )
            rl.addWidget(dele)

            def on_click(_e, pid=prof["id"]):
                # 点已有的一行 → 退出新增模式。
                # 不然 _is_adding 还挂着 True，刷新逻辑会一直
                # 认为"用户正在新增"，永不回填。
                self._is_adding = False
                self._selected_pid = pid
                self._render_profiles()
                cur = next((x for x in self._profiles if x["id"] == pid), None)
                if cur:
                    self._fill_editor(cur)

            row.mousePressEvent = on_click
            self.profile_box.addWidget(row)

    def _set_new_mode(self, is_new: bool) -> None:
        """切换编辑区的模式。

        is_new=True   正在新增 → 显示快速填、测试连接、保存
        is_new=False  在看已有的一套 → 全藏起来

        为什么看已有的要藏：
            那套早就存好了，还摆着"快速填某家"的按钮和"保存"，
            用户会以为每点一下都得存一次。
        """
        for w in (
            getattr(self, "quick_row_host", None),
            getattr(self, "action_row_host", None),
        ):
            if w is not None:
                w.setVisible(is_new)

    def _add_profile(self) -> None:
        """切到"新增"状态：清空表单，等用户填。

        ⚠️ 这里**不建配置**。
        ⚠️ 配置在点「保存」时才真的建。

        为什么：
            原来点一下就在后端建一条空配置，列表里立刻多一个 tab。
            用户点了一下又不想建，就白留一个空配置。
            现在：先填，填完保存才落地。
        """
        self._is_adding = True
        self._selected_pid = ""          # 没有绑定的那一套
        self._set_new_mode(True)

        self.edit_title.setText("新增模型")
        self.ed_base.clear()
        self.ed_key.clear()
        self.ed_key.setPlaceholderText("粘贴你的 API Key")
        self.lbl_key_state.setText("")
        self.cmb_engine_model.clear()
        self._models_loaded_for = ""

        self.btn_del_profile.setEnabled(False)
        self.lbl_engine.setText("填好地址和 Key，模型名会自动出来")
        self.lbl_engine_now.setText("正在：新增")

    def _delete_profile(self) -> None:
        pid = getattr(self, "_selected_pid", "")
        if not pid:
            return
        prof = next((x for x in getattr(self, "_profiles", []) if x["id"] == pid), None)
        name = prof.get("name") if prof else pid

        if QMessageBox.question(
            self, "确认", f"删掉「{name}」这套配置？"
        ) != QMessageBox.Yes:
            return

        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self.lbl_engine.setText("已删除")
                self._selected_pid = ""
                self._load_profiles()
            else:
                self.lbl_engine.setText("删除失败")

        self.api.request_once("/api/engine/profiles",
                              {"action": "delete", "id": pid},
                              callback=done)

    def _switch_profile(self, pid: str) -> None:
        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self.lbl_engine.setText("已切换")
                self._load_profiles()
            else:
                self.lbl_engine.setText("切换失败")

        self.api.request_once("/api/engine/profiles",
                              {"action": "switch", "id": pid},
                              callback=done)

    def _toggle_profile(self, pid: str, currently_on: bool) -> None:
        """开/关一套模型配置。

        ⚠️ 这是"开关"，不是"切换"。

            以前只有一个 [启用] 按钮，点了会把"当前那套"换成它——
            等于任何时刻只能有一套在跑。

            但实际用起来不是这样：同时开 DeepSeek 和 Kimi 很常见，
            两个各跑各的，任务按需分配。所以每套要独立开关。

            "当前默认用哪套"是另一件事，点行本身来切。
        """
        target = not currently_on

        # 不允许全关。
        #
        # 为什么：全关之后任何任务都跑不了，用户会以为程序坏了，
        # 而且他还得先找到这里才能修好。
        on_count = sum(1 for p in (self._profiles or []) if p.get("enabled", True))
        if currently_on and on_count <= 1:
            # 用弹窗，不用页面底部那行小字。
            #
            # 实测：那行字太小、位置偏下，用户点了没反应会以为坏了。
            QMessageBox.information(
                self,
                "得留一套",
                "至少留一套开着，不然没东西跑任务。\n\n"
                "想换的话，先把新那套打开，再关这套。",
            )
            return

        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self._load_profiles()
            else:
                self.lbl_engine.setText(
                    f"没改成功：{(data or {}).get('error', '未知原因')}"
                )

        self.api.request_once(
            "/api/engine/profiles",
            {"action": "update", "id": pid, "enabled": target},
            callback=done,
        )

    def _delete_profile_by_id(self, pid: str) -> None:
        """从列表里直接删一套。"""
        # 删掉的可能正是"正在新增"时想依附的那套，一并复位
        if getattr(self, "_selected_pid", "") == pid:
            self._selected_pid = ""
            self._is_adding = False
        prof = next((x for x in (self._profiles or []) if x["id"] == pid), None)
        name = (prof or {}).get("name") or "这套"

        r = QMessageBox.question(
            self,
            "删除配置",
            f"删掉「{name}」？\n\n只删这套的地址和 Key，已经做出来的任务不受影响。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if r != QMessageBox.Yes:
            return

        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self._load_profiles()
            else:
                self.lbl_engine.setText(
                    f"没删掉：{(data or {}).get('error', '未知原因')}"
                )

        self.api.request_once(
            "/api/engine/profiles",
            {"action": "delete", "id": pid},
            callback=done,
        )

