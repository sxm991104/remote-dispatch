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

import os

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


def _ver_gt(a: str, b: str) -> bool:
    """版本号 a 比 b 新吗。只认点分隔的数字，别的当 0。

    ⚠️ 不要用字符串比较 —— "1.10.0" < "1.9.0" 那种坑。
    """
    def _parts(v: str) -> list[int]:
        out: list[int] = []
        for x in (v or "").replace("-", ".").split("."):
            n = ""
            for ch in x:
                if ch.isdigit():
                    n += ch
                else:
                    break
            out.append(int(n) if n else 0)
        return out

    pa, pb = _parts(a), _parts(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return pa > pb

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

        # 分隔线 + 版本行（设计稿里和开机自启同一张卡）
        div = QFrame(); div.setFixedHeight(1)
        div.setStyleSheet(f"background:{C_LINE};")
        al.addWidget(div)

        vrow = QHBoxLayout(); vrow.setSpacing(12)
        vcol = QVBoxLayout(); vcol.setSpacing(0)
        vh = QLabel("版本"); vh.setFont(font(11)); vh.setStyleSheet(f"color:{C_DIM2};")
        vtxt = QLabel(self._read_version_text()); vtxt.setFont(font(11.5, QFont.DemiBold))
        vcol.addWidget(vh); vcol.addWidget(vtxt)
        vrow.addLayout(vcol, 1)
        self.btn_check_update = QPushButton("检查更新")
        self.btn_check_update.setObjectName("ghost")
        self.btn_check_update.setCursor(Qt.PointingHandCursor)
        self.btn_check_update.clicked.connect(self._check_updates)
        vrow.addWidget(self.btn_check_update, 0, Qt.AlignTop)
        al.addLayout(vrow)

        lay.addWidget(acard)

    def _read_version_text(self) -> str:
        """读仓库根 VERSION，拼上 atrium 版本（读不到就只显示主版本）。"""
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(os.path.dirname(here))
        ver = ""
        try:
            with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
                ver = f.read().strip()
        except OSError:
            ver = "?"
        return f"dsh-bridge {ver}"

    def _check_updates(self) -> None:
        """真的去 GitHub 查一下有没有新版本。

        ⚠️ 原来这里只弹一句"当前已是本地最新版本"—— 什么都不查。
            用户点一百次都是这句话，等于骗人。

        ⚠️ 查的时候走后台线程。
            urlopen 最慢能卡十几秒，放在主线程里界面会整个冻住。

        ⚠️ 查不到**不要说"已是最新"**。
            网络断了 / 被墙了 / 仓库没发 Release —— 这些都查不到，
            但跟"确实是最新"是两回事。分开说。
        """
        import threading

        from PySide6.QtWidgets import QMessageBox

        # ⚠️ 别用 QMetaObject.invokeMethod 送 lambda。
        #
        #    它只接受「成员方法名字符串」，不接受 Python 函数：
        #        TypeError: invokeMethod(MainWindow, function, ConnectionType)
        #                called with wrong argument types
        #
        #    （2026-11-05 点「检查更新」直接炸，就是写错在这个。）
        #
        #    后台线程要回主线程改界面，最稳的是**自定义信号**。
        REPO = "sxm991104/remote-dispatch"
        cur = self._read_version_num()

        self.btn_check_update.setEnabled(False)
        self.btn_check_update.setText("检查中…")

        def finish(payload) -> None:
            title, msg = payload
            try:
                self.btn_check_update.setEnabled(True)
                self.btn_check_update.setText("检查更新")
            except Exception:  # noqa: BLE001
                pass
            QMessageBox.information(self, title, msg)

        # 后台线程不能直接调 finish（碰界面会崩），
        # 排进主线程队列，由 _drain_main_queue 执行。
        def post(payload):
            q = getattr(self, "_main_queue", None)
            if q is None:
                return
            q.append(lambda: finish(payload))

        def work() -> None:
            import json
            import urllib.request

            try:
                req = urllib.request.Request(
                    f"https://api.github.com/repos/{REPO}/releases/latest",
                    headers={"User-Agent": "dsh-bridge-updater"},
                )
                with urllib.request.urlopen(req, timeout=12) as r:
                    d = json.loads(r.read().decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                post((
                    "检查更新",
                    f"没查到版本信息（{type(e).__name__}）。\n\n"
                    "可能是网络不通，或者仓库还没发过 Release。\n"
                    f"当前版本：{cur}",
                ))
                return

            tag = str(d.get("tag_name") or "").lstrip("vV")
            url = str(d.get("html_url") or "")

            if not tag:
                post(
                    ("检查更新", f"接口没给版本号。当前版本：{cur}")
                )
                return

            newer = _ver_gt(tag, cur)
            post((
                "检查更新",
                (f"有新版本：{tag}\n当前：{cur}\n\n{url}"
                 if newer else
                 f"已经是最新的。\n当前：{cur}\n最新：{tag}"),
            ))

        threading.Thread(target=work, daemon=True).start()

    def _read_version_num(self) -> str:
        """只读版本号本身（不带 "dsh-bridge " 前缀）。"""
        here = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(os.path.dirname(here))
        try:
            with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""


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
            # ⚠️ 记下"真实状态"。_toggle_autostart 以它为基准取反 ——
            #    不能读按钮的 isChecked()，那个在点击瞬间已经被 Qt 翻过了。
            self._auto_current = on
            self._apply_autostart_view(on)

        self.api.request_once("/api/autostart", callback=done)

    def _toggle_autostart(self) -> None:
        """点了开关：按**点之前**的状态取反。

        ⚠️ 这里原来写的是 `want = not self.switch_auto.isChecked()`，
            但那个按钮是 setCheckable(True) 的 —— 点下去的一瞬间
            Qt 已经把勾选状态翻转了，所以 isChecked() 读到的**已经是新值**。
            再取一次反，等于设成了"用户想要的反面"。

            表现：想开 → 传了关；想关 → 传了开。
            服务端老实照着做，然后 _refresh_autostart() 一刷新，
            按钮又弹回原样 —— 用户看到的就是"点了没反应"。

            实测（2026-11-05）：老爸点了半天，一直显示开着，实际也没生效。

        ⚠️ 现在用 sender() 拿点击前的状态？Qt 里没有这个接口。
            可靠的做法是**以服务端当前状态为准**：
            点之前先记下来，用 self._auto_current（_refresh 时同步好的）。
        """
        # 以「刷新时记下的真实状态」为基准，不读按钮的勾选状态
        cur = bool(getattr(self, "_auto_current", False))
        want = not cur

        # 按钮立刻反映用户的意图（等服务端回来再校正）
        self.switch_auto.setChecked(want)

        def done(data):
            # ⚠️ 这一整段都要防"控件已经被销毁"。
            #
            #    页面是懒建的，而且这个回调是**跨线程排队**回来的 ——
            #    排队期间用户可能已经切走、页面被回收。
            #    这时候碰 self.lbl_auto 直接 AttributeError，
            #    异常在 Qt 信号回调里吞掉，表现就是"回调跑了但界面没变"。
            #
            #    实测（2026-11-05）：点开关，磁盘上的 lnk 真删掉了，
            #    界面还写着"开机自启 · 开"—— 就是死在这。
            try:
                ok = bool((data or {}).get("ok"))
                lbl = getattr(self, "lbl_auto", None)
                if lbl is not None:
                    if not ok:
                        lbl.setText((data or {}).get("error") or "设置失败")
                    else:
                        lbl.setText((data or {}).get("note") or "")
            except Exception:  # noqa: BLE001
                pass

            # 用**这次返回里的 enabled** 直接定状态，不要再发一次查询。
            #
            # 原来这里调 _refresh_autostart() —— 又发一个同路径的请求。
            # 而 Api 层对同一路径有"去重"：新请求可能被判定成重复
            # 直接丢弃，回调永远不回来。
            if "enabled" in (data or {}):
                self._auto_current = bool(data["enabled"])
                self._apply_autostart_view(self._auto_current)
            else:
                self._refresh_autostart()

        self.api.request_once("/api/autostart", {"on": want}, callback=done)

    def _apply_autostart_view(self, on: bool) -> None:
        """把开关画成这个状态（不发请求）。

        ⚠️ 控件可能不存在（页面还没建 / 已经被回收），所以全用 getattr。
        """
        try:
            sw = getattr(self, "switch_auto", None)
            if sw is None:
                return
            sw.setChecked(on)
            sw.setObjectName("switchon" if on else "switchoff")
            sw.setText("开机自启 · 开" if on else "开机自启 · 关")
            sw.style().unpolish(sw)
            sw.style().polish(sw)
        except Exception:  # noqa: BLE001
            pass

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


