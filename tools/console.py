"""统一处理 stdout 编码：Windows CI 上默认是 cp1252，没有 emoji。

为什么需要这个
    实测（GitHub Actions 的 Windows runner）：

        print("✅ 端到端测试")  →  UnicodeEncodeError
        'charmap' codec can't encode characters

    Windows 英文系统的默认编码是 cp1252，里面没有 ✅ ✗ 这些符号。
    Ubuntu 上默认 UTF-8，所以那边一直是绿的。

    一处崩了，整段测试就没跑 —— 而这跟被测代码毫无关系。

做法
    mark_utf8()  把 stdout/stderr 的编码改成 UTF-8，errors="replace"

    errors="replace" 是兜底：真写不出去也只会显示成 ?，不会把整段拖死。

⚠️ 为什么不重新包一层 TextIOWrapper
    试过，会出事：
        - 多次调用会反复包，最后 "lost sys.stderr"
        - 有些环境（pytest 的捕获、被管道接了）没有 buffer
    所以只用 reconfigure —— 那是 TextIOWrapper 自带的方法，最稳。
"""

from __future__ import annotations

import sys

_DONE = False


def mark_utf8() -> None:
    """把 stdout/stderr 的编码改成 UTF-8。重复调用无副作用。"""
    global _DONE
    if _DONE:
        return
    _DONE = True

    for name in ("stdout", "stderr"):
        s = getattr(sys, name, None)
        if s is None:
            continue
        fn = getattr(s, "reconfigure", None)
        if fn is None:
            # 不是 TextIOWrapper（StringIO / 被替换过的流），什么都不做
            continue
        try:
            fn(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


install = mark_utf8
