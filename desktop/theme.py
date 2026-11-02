"""界面的配色和几个小工具。

## 为什么单独拆出来

    原来这些散在 desktop/app.py 里（4311 行那个文件）。
    拆出来之后：

        · 改配色只开这一个文件
        · 别的模块要用颜色，import 这里，不用反向依赖 app.py
        · 这里的依赖只有 PySide6 和标准库，**没有任何项目内部的依赖**

    ⚠️ 这个文件要保持"零项目依赖"。
        一旦它 import 了别的东西，就会出现循环引用 ——
        因为它是最底层，所有界面模块都要用到它。

## 拆的时候只搬代码，注释原样保留
"""

from __future__ import annotations


from PySide6.QtGui import QFont


# ────────────────────────────────────────────────────────────────────
# 配色：偏暖的深灰蓝，中间调
# ────────────────────────────────────────────────────────────────────
C_BG      = "#f6f6f4"      # 页面底（近白，带一点暖灰，不刺眼）
C_SURFACE = "#ffffff"      # 卡片（纯白，浮在底色上）
C_RAISED  = "#f0f0ed"      # 悬停 / 抬起来
C_INPUT   = "#fafaf8"      # 输入框内底

C_LINE    = "#e4e3df"      # 细边（很淡，靠明度分层不靠线）
C_LINE_HI = "#d2d0cb"      # 悬停边

C_TEXT    = "#2b2a28"      # 主文字（近黑，带暖，不用纯黑）
C_TEXT2   = "#4f4d49"      # 次要
C_DIM     = "#7d7a75"      # 描述
C_DIM2    = "#a5a29c"      # 最弱

C_OK      = "#1f9c5f"      # 绿（白底上要够深才看得清）
C_RUN     = "#2b7fd4"      # 蓝
C_BAD     = "#d64545"      # 红
C_WARN    = "#c07d10"      # 琥珀
C_ACCENT  = "#5b5bd6"      # 主强调（靛蓝，白底上稳重）
C_ACCENT2 = "#4a4ac4"      # 强调悬停
C_ACCENT3 = "#3d3da8"      # 强调按下
C_GRAD_A  = "#5b5bd6"      # 渐变色起
C_GRAD_B  = "#8b5cf6"      # 渐变色止

STATUS_TEXT = {
    "ok": "完成", "running": "运行中", "pending": "排队",
    "timeout": "超时", "error": "失败", "empty": "无输出", "cancelled": "已停",
}


def status_color(s: str) -> str:
    if s == "ok":
        return C_OK
    if s == "running":
        return C_RUN
    if s == "pending":
        return C_DIM2
    return C_BAD


def money(v) -> str:
    return f"¥{v:.2f}" if isinstance(v, (int, float)) else ""


def toks(v) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    return f"{v/10000:.1f} 万" if v >= 10000 else f"{v:,.0f}"


def secs(v) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    return f"{v:.0f} 秒" if v < 60 else f"{v/60:.1f} 分"


def font(size: int = 13, weight: int = 400) -> QFont:
    f = QFont()
    f.setFamilies(["Microsoft YaHei UI", "Segoe UI", "PingFang SC"])
    f.setPixelSize(size)
    f.setWeight(QFont.Weight(weight))
    return f


# ────────────────────────────────────────────────────────────────────
# 后台请求
# ────────────────────────────────────────────────────────────────────



# ── 估文字宽度 ──
#   原来它夹在 api 那堆函数中间，切 theme 时被漏下了。
#   它是纯工具函数，属于这一层。
def _text_width(text: str, size: int = 11) -> int:
    """粗估一段文字的像素宽度。

    为什么要估：
        名字列的宽度要按最长的那个定，不然每行宽窄不一，看着乱。

        中文字符大约是字号的 1 倍宽，英文和数字大概 0.55 倍。
        估个大概就行——差几个像素看不出来，差一整行才难看。
    """
    w = 0.0
    for ch in str(text):
        w += 1.0 if ord(ch) > 0x2E80 else 0.55
    return int(w * size)


