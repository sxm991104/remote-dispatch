"""检查 workflow 里所有 bash 片段能不能过 bash -n。

## 为什么要有这个

    2026-11-03：我在 workflow 里插了一段 grep，
    里面有一个**弯引号** `’`（从中文排版里带进来的）。

    bash 解析时引号不配对 → 整个 run 块语法错误 → exit code 2
    → **四个平台全炸，一条测试都没跑**。

    而 CI 只回一句 exit code 2，看不出是哪儿。
    我还在本地跑了一遍全是绿的 —— 因为改的是 workflow，不是代码。

    这种错不该靠人眼盯。bash -n 一秒钟就能查出来。

## 做什么

    从 tests.yml 里抠出每个 `run: |` 块，
    交给 bash -n 检查语法。

    ⚠️ 只检查 bash 块（shell: bash 或默认）。
       pwsh 块跳过 —— 那是另一套语法。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows", "tests.yml")


def main() -> int:
    if not os.path.exists(WF):
        print("  没有 workflow 文件，跳过")
        return 0

    with open(WF, encoding="utf-8") as f:
        lines = f.read().splitlines()

    # 抠出每个 run: | 块
    blocks: list[tuple[int, str, str]] = []   # (起始行号, shell, 内容)

    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)(- name:|run:)", lines[i])
        if lines[i].strip() == "run: |":
            start = i
            ind = len(lines[i]) - len(lines[i].lstrip())
            body_ind = None
            j = i + 1
            body: list[str] = []
            while j < len(lines):
                s = lines[j]
                if not s.strip():
                    body.append("")
                    j += 1
                    continue
                cur = len(s) - len(s.lstrip())
                if body_ind is None:
                    body_ind = cur
                if cur < body_ind:
                    break
                body.append(s[body_ind:])
                j += 1

            # 往前找 shell: pwsh
            shell = "bash"
            for k in range(max(0, start - 3), start):
                if "shell: pwsh" in lines[k]:
                    shell = "pwsh"
                elif "shell: python" in lines[k]:
                    shell = "python"
            blocks.append((start + 1, shell, "\n".join(body)))
            i = j
            continue
        i += 1

    print(f"  找到 {len(blocks)} 个 run 块")

    # 用 bash -n 检查 bash 块
    bash = _find_bash()
    if bash is None:
        print("  ⚠️ 找不到 bash，跳过语法检查")
        return 0

    bad = 0
    for lineno, shell, body in blocks:
        if shell != "bash":
            continue
        # ⚠️ GitHub 的 ${{ }} 表达式不是 bash，先替换成占位符
        cleaned = re.sub(r"\$\{\{[^}]*\}\}", "EXPR", body)
        r = subprocess.run(
            [bash, "-n"],
            input=cleaned.encode("utf-8"),
            capture_output=True,
            timeout=30,
        )
        if r.returncode != 0:
            bad += 1
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
            print(f"  ❌ 第 {lineno} 行的 run 块语法错：")
            for ln in err.splitlines()[:6]:
                print(f"       {ln}")

    if bad:
        print(f"\n  共 {bad} 个块有语法错")
        return 1

    print("  ✅ 所有 bash 块语法正确")
    return 0


def _find_bash() -> str | None:
    """找一个能用的 bash。"""
    import shutil

    for name in ("bash", "bash.exe"):
        p = shutil.which(name)
        if p:
            return p

    # Windows 上常见位置
    for p in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        "/bin/bash",
        "/usr/bin/bash",
    ):
        if os.path.exists(p):
            return p

    return None


if __name__ == "__main__":
    sys.exit(main())
