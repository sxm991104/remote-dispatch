"""打印项目现在的真实数字。

## 为什么要有这个

    文档里到处写着「346 个回归测试」「55 个 .py，25680 行」
    「137 条暴力载荷」—— 这些数字**是手写的**。

    手写的数字一周后就失准，而且失准了没人知道。
    实测（2026-11-02）同步到的就有五处对不上：

        REVIEW_GUIDE   55 个 .py / 25680 行   实际 69 个 / 32236 行
        README         346 个回归             实际 373
        TESTS          338 个回归 / 322 条单元 实际 373 / 332
        ROADMAP        328 条回归             实际 373
        CHANGELOG      373 / 367              对的

    所以：**数字只有一个地方产生 —— 这里。**
    文档引用它，或者干脆不写具体数。

## 用法

    python tools/stats.py          # 直接看
    python tools/stats.py --md     # 输出一段 markdown，贴进文档
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 不算进统计的目录
SKIP_DIRS = {
    "__pycache__", ".git", "state", "workspaces", "node_modules",
    ".pytest_cache", ".mypy_cache",
}


def count_py() -> tuple[int, int]:
    """(py 文件数, 总行数)。"""
    n = 0
    lines = 0
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dp, fn)
            n += 1
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    lines += sum(1 for _ in f)
            except OSError:
                pass
    return n, lines


def count_by_area() -> dict[str, tuple[int, int]]:
    """按目录分组：(文件数, 行数)。"""
    out: dict[str, list[int]] = {}
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, ROOT)
            area = rel.split(os.sep)[0] if os.sep in rel else "（根目录）"
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    n_lines = sum(1 for _ in f)
            except OSError:
                n_lines = 0
            cur = out.setdefault(area, [0, 0])
            cur[0] += 1
            cur[1] += n_lines
    return {k: (v[0], v[1]) for k, v in out.items()}


def biggest_files(top: int = 10) -> list[tuple[str, int]]:
    """最大的几个 .py。"""
    out = []
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dp, fn)
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    n = sum(1 for _ in f)
            except OSError:
                continue
            out.append((os.path.relpath(p, ROOT), n))
    out.sort(key=lambda x: -x[1])
    return out[:top]


# ⚠️ 为什么这里**不**去数源码算测试条数
#
#     第一版是数源码：数 brute_force.py 里 `("名字", "命令")` 的元组、
#     数 redteam_ext.py 里 ck( 的调用。
#
#     结果是错的：
#         红队断言数出 61 条   实际跑出来 332 条
#         权限验证数出 12 条   实际 14 条
#
#     原因：这些测试大量是**循环批量构造**的
#     （for name, cmd in CASE_LIST: ok(...)），
#     源码里只有一个调用点，跑起来是几十上百条。
#
#     数源码永远对不上。条数只能**问跑一次的它自己**。
#
#     所以这里只报规模（文件数、行数、哪个文件多大），
#     测试条数去跑 `python tests/run_all.py`，那是唯一的真相。


def main() -> int:
    n_py, n_lines = count_py()
    area = count_by_area()
    big = biggest_files()

    as_md = "--md" in sys.argv

    if as_md:
        print(f"- Python 文件：**{n_py}** 个")
        print(f"- 总行数：**{n_lines}** 行")
        print()
        print("> ⚠️ 测试条数以 `python tests/run_all.py` 的实际输出为准，")
        print(">    不要手写进文档 —— 会失准。")
        return 0

    print("=" * 62)
    print("  规模")
    print("=" * 62)
    print(f"  Python 文件   {n_py} 个")
    print(f"  总行数        {n_lines} 行")
    print()
    print("  按目录：")
    for k in sorted(area):
        f, l = area[k]
        print(f"    {k:22} {f:3} 个  {l:6} 行")

    print()
    print("=" * 62)
    print("  最大的几个文件（重构时先看这几个）")
    print("=" * 62)
    for rel, n in big:
        bar = "█" * max(1, n // 200)
        print(f"  {rel:34} {n:5} 行  {bar}")

    print()
    print("=" * 62)
    print("  测试条数")
    print("=" * 62)
    print("  去跑 `python tests/run_all.py` —— 那里是唯一的真相。")
    print()
    print("  ⚠️ 不要从源码数，也不要手写进文档：")
    print("     这些测试大量是循环批量构造的，源码里一个调用点跑起来几十条。")
    print("     实测数源码得到 61 条，实际跑出来 332 条。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
