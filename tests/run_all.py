"""一键跑完全部回归测试。

用法：
    python tests/run_all.py

或者直接：
    pytest tests/ -v

这个脚本的好处是不用装 pytest 的命令行参数也能跑，
而且会打印一份"哪个模块测了多少条"的汇总。
"""

from __future__ import annotations

# ⚠️ Windows CI 上 stdout 的默认编码是 cp1252（英文系统），
#    里面没有 ✅ ✗ 这些符号，print 会直接抛 UnicodeEncodeError。
#    实测：整个测试一条都没跑就崩了，而 Ubuntu 上一直是绿的。
#
#    下面这两行必须在任何 print 之前执行。
try:
    from tools.console import mark_utf8 as _mark_utf8
except ImportError:
    import os as _os0, sys as _sys0
    _sys0.path.insert(0, _os0.path.dirname(_os0.path.dirname(_os0.path.abspath(__file__))))
    from tools.console import mark_utf8 as _mark_utf8
_mark_utf8()

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# 项目自己的临时目录。不走系统的 Temp ——
# 那个目录的权限在有些机器上会坏，见下面 --basetemp 的说明。
_LOCAL_TMP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_tmp"
)

# ⚠️ 先建出父目录。
#    pytest 的 --basetemp 只会建最后一级，
#    父目录不存在会直接 FileNotFoundError。
try:
    os.makedirs(_LOCAL_TMP, exist_ok=True)
except OSError:
    pass

# pytest 的 basetemp，指到项目自己的 _tmp 下。
#
# 为什么不让它用默认的 %LOCALAPPDATA%\Temp\pytest-of-<user>：
#     那个目录在有些机器上权限会坏（连 icacls 都读不了），
#     结果 tmp_path 建不了、收尾清理抛 PermissionError，
#     测试本身全过但整个模块被判失败。
#
# 实测（2026-11-04）：12 个模块全红，全是这一个原因。
_BASE_TMP = os.path.join(_LOCAL_TMP, "pytest-base")

ROOT = os.path.dirname(HERE)

# ⚠️ 中文 Windows 的控制台默认是 GBK。
#    脚本里用了 ✅ ❌ 这些符号，GBK 打不出来会直接崩：
#        UnicodeEncodeError: 'gbk' codec can't encode character
#
#    而"别人 clone 下来跑不起来"正是最劝退的事。
#    所以这里强制把 stdout 换成 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    print("=" * 66)
    print("dsh-bridge 回归测试")
    print("=" * 66)

    # 检查 pytest
    try:
        import pytest  # noqa: F401
    except ImportError:
        print("\n  ❌ 没装 pytest。装一下：")
        print("     python -m pip install pytest")
        return 1

    # 先打环境信息。
    #
    # 为什么：CI 失败的时候，第一件想知道的事就是"跑在什么环境里"。
    # 不打出来，就只能靠猜——今天在这上面浪费了太多时间。
    import platform as _pf

    print(f"{'=' * 66}")
    print(f"dsh-bridge 回归测试")
    print(f"{'=' * 66}")
    print(f"  平台: {sys.platform} / {_pf.machine()}")
    print(f"  Python: {sys.version.split()[0]}  ({sys.executable})")
    print(f"  工作目录: {os.getcwd()}")
    print(f"  APPDATA: {os.environ.get('APPDATA') or '(未设置)'}")
    print(f"  HOME: {os.environ.get('HOME') or os.path.expanduser('~')}")
    print()

    t0 = time.time()

    # 按模块跑，这样能看到每个模块的统计
    modules = [
        ("路径安全", "tests/test_safe_path.py"),
        ("危险操作验证码", "tests/test_guard.py"),
        ("动作级守卫", "tests/test_action_guard.py"),
        ("实战 bug", "tests/test_field_bugs.py"),
        ("接口安全", "tests/test_security.py"),
        ("引擎核心", "tests/test_engine.py"),
        ("并发与调度", "tests/test_concurrency.py"),
        ("产出文件", "tests/test_outputs.py"),
        ("压力测试", "tests/test_stress.py"),
        # 静态检查：桌面端有没有"调用了但没定义"的方法。
        # 这类错 py_compile 拦不住（语法是对），pytest 也拦不住（没人 import 界面），
        # 但一炸就是整个桌面端打不开。2026-11-02 一天里犯了两次。
        ("界面静态检查", "tests/test_ui_static.py"),
        # 扫「用了但没定义也没 import」的名字 ——
        # 拆文件时最常犯的错，py_compile 完全看不出来。
        ("名字静态检查", "tests/test_static_names.py"),
        # 真的把窗口建出来、切一遍四个页面。
        # ⚠️ 上面那两条静态检查拦不住"运行某个函数才炸"的错：
        #    tools/deps.py 缺 _userdirs 的 import，静态全过，
        #    一点「环境」页就崩 —— 从 1.3.3 潜伏到现在。
        #    没有 PySide6 会 skip，CI 里装了所以会真跑。
        ("桌面端启动", "tests/test_desktop_boots.py"),
    ]

    # ── workflow 的 bash 片段语法 ──
    #
    # ⚠️ 2026-11-03：我在 tests.yml 里插了一段 grep，
    #    里面混进一个**弯引号** ’（从中文排版带进来的）。
    #
    #    bash 解析时引号不配对 → 整个 run 块语法错 → exit code 2
    #    → **四个平台全炸，一条测试都没跑**。
    #
    #    而本地跑 run_all 全是绿的 —— 因为改的是 workflow，不是代码。
    #    这种错不该靠人眼盯，bash -n 一秒钟就能查出来。
    print(f"\n{'─' * 66}")
    print("  workflow 语法")
    print(f"{'─' * 66}")
    try:
        import subprocess as _sp

        _r = _sp.run(
            [sys.executable, os.path.join(ROOT, "tools", "check_workflow.py")],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        for _ln in (_r.stdout or "").splitlines():
            print(f"  {_ln.strip()}")
        if _r.returncode != 0:
            total_fail += 1
            failed_modules.append("workflow 语法")
        else:
            print("    ✅ 全过")
    except Exception as _e:  # noqa: BLE001
        print(f"  ⚠️ 检查器跑不起来：{_e}")

    total_pass = 0
    total_fail = 0
    failed_modules = []
    # 「全跳过」的模块 —— 不算失败，但要让人看见。
    # 否则没装 PySide6 的环境会一直以为桌面端测过了。
    skipped_modules = []

    for label, path in modules:
        full = os.path.join(ROOT, path)
        if not os.path.exists(full):
            print(f"\n  ⚠️ {label}：文件不存在（{path}）")
            continue

        print(f"\n{'─' * 66}")
        print(f"  {label}")
        print(f"{'─' * 66}")

        r = subprocess.run(
            [sys.executable, "-m", "pytest", full, "-v", "--tb=short",
             "-p", "no:cacheprovider", "--basetemp", _BASE_TMP],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        out = r.stdout + r.stderr

        # 从输出里找统计行
        for line in out.splitlines():
            if " passed" in line or " failed" in line or " error" in line:
                print(f"  {line.strip()}")
                break

        # ⚠️ 「全部跳过」不等于失败，也不等于通过。
        #
        #    pytest 在「0 个测试被收集、全部 skip」时返回 1，
        #    和真失败一个退出码。于是这一条被算进 failed_modules，
        #    报出来是「失败模块：桌面端启动」，但实际 0 失败 0 通过 ——
        #    看的人一头雾水。
        #
        #    而且更危险的是反过来：如果它静默算通过，
        #    没装 PySide6 的环境就永远测不到界面。
        #
        #    所以：看输出里有没有 skipped，有就明确报出来。
        if "skipped" in (r.stdout or "") and r.returncode != 0:
            skipped_modules.append(label)
        elif r.returncode != 0:
            failed_modules.append(label)

            # ⚠️ 原来只打 "FAILED xxx" 这一行，看不到为什么失败。
            #
            #    在 CI 里就抓瞎了：只知道哪个测试红了，
            #    不知道是断言失败、ImportError 还是环境问题。
            #
            #    而 CI 日志只能从网页看（API 要 token），
            #    所以必须把足够的信息打到 stdout 里。
            #
            #    现在：失败时打完整的 traceback（最后 40 行）。
            print(f"    ---- {label} 失败详情 ----")
            for line in out.splitlines()[-40:]:
                print(f"    {line}")
        else:
            print("    ✅ 全过")

        # 数一下
        import re
        m = re.search(r"(\d+) passed", out)
        if m:
            total_pass += int(m.group(1))
        m = re.search(r"(\d+) failed", out)
        if m:
            total_fail += int(m.group(1))

    # ── 红队扩展（外部独立脚本，不在 pytest 列表里）──
    #
    # 这份是别人写的（见 tests/redteam_ext.py 头部说明）。
    # 它是独立脚本，不是 pytest 用例，所以单独跑。
    print(f"\n{'─' * 66}")
    print("  红队扩展")
    print(f"{'─' * 66}")

    rt = os.path.join(ROOT, "tests", "redteam_ext.py")
    if os.path.exists(rt):
        r = subprocess.run(
            [sys.executable, rt, ROOT],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        for line in (r.stdout or "").splitlines():
            if "合计" in line:
                print(f"  {line.strip()}")

        # ⚠️ 这里原来只打印，不计数 —— 是假绿。
        #
        #    有人指出来的问题：
        #        run_all.py 跑了 redteam_ext，打了"合计"那行，
        #        用 known 过滤掉我们不同意的条目，然后……
        #        没有 if r.returncode，没有 failed_modules.append，
        #        没有 total_fail +=
        #
        #    结果：322 条断言是装饰性的。
        #    哪天 F-01 符号链接那个洞被改回去，CI 照样绿。
        #
        #    这跟我们自己写在 PITFALLS 里的话是一回事：
        #    「假成功比失败更害人」。
        #    加 CI 是为了消灭假绿，结果在 CI 里造了一个假绿。
        #
        #    现在：白名单之外的真失败要计进 failed_modules，
        #    让退出码变红。分歧照样显式记录在 known 里，
        #    但"分歧"和"失败"是两件事，不能混。
        # ⚠️ 豁免取消：那两条分歧已经各自在用例里改对了（::ffff 判本机是对的、
        #    提示注入派活层本来就不该按文字判断）。按标签前缀豁免会掩盖将来的真问题。
        known: tuple[str, ...] = ()
        rt_real_fail = 0
        for line in (r.stdout or "").splitlines():
            t = line.strip()
            if t.startswith("·") and not any(k in t for k in known):
                rt_real_fail += 1
                print(f"    {t}")

        # ⚠️ 判断顺序要小心。
        #
        #    第一版写的是：先看 rt_real_fail，再看 returncode。
        #    结果正常情况也变红了 —— 因为红队脚本有 6 条我们不同意的失败，
        #    它自己 sys.exit(1)，而过滤之后 rt_real_fail 是 0，
        #    于是走了"异常"分支。
        #
        #    正确的顺序：
        #        1. 先看过滤后有没有真失败
        #        2. 没有真失败，而且退出码非 0 → 才怀疑脚本自己崩了
        #
        #    而且"脚本崩了"的判据不能只看退出码 —— 红队脚本
        #    有失败条目时本来就返回 1。要区分"有失败"和"崩了"，
        #    得看它有没有把"合计"那行打出来：
        #        打出来了   → 跑完了，失败就是失败
        #        没打出来   → 中途崩了
        printed_summary = any(
            "合计" in ln for ln in (r.stdout or "").splitlines()
        )

        if rt_real_fail:
            failed_modules.append(f"红队扩展({rt_real_fail})")
            total_fail += rt_real_fail
        elif r.returncode != 0 and not printed_summary:
            # 没打印"合计"就退出了 → 脚本自己崩了
            failed_modules.append("红队扩展(异常)")
            total_fail += 1
            print(f"    ⚠️ 红队脚本退出码 {r.returncode}，且没打印合计行")
            tail = (r.stdout or "")[-800:] + (r.stderr or "")[-800:]
            for ln in tail.splitlines()[-20:]:
                print(f"       {ln}")
        elif printed_summary and any(k for k in known):
            # 有失败，但全在白名单里 —— 这是我们明确不同意的，不算红
            pass

    # ── 暴力攻击测试 ──
    #
    # 单独跑，因为它不是 pytest 用例（是自己写的一个脚本）。
    # 它打的是"挡不挡得住"，跟上面那些"功能对不对"是两回事。
    print(f"\n{'─' * 66}")
    print("  暴力攻击测试")
    print(f"{'─' * 66}")

    bf = os.path.join(ROOT, "tests", "brute_force.py")
    if os.path.exists(bf):
        r = subprocess.run(
            [sys.executable, bf],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        for line in (r.stdout or "").splitlines():
            if "结果：" in line or "失败清单" in line:
                print(f"  {line.strip()}")
        for line in (r.stdout or "").splitlines():
            if line.strip().startswith("[绕过]") or line.strip().startswith("[误伤]"):
                print(f"    {line.strip()}")
        if r.returncode != 0:
            failed_modules.append("暴力攻击")

    # ── 权限验证（改权限要过验证码）──
    #
    # ⚠️ 这一节以前**从来没被跑过**。
    #
    #    它原来叫 tests/test_perm_verify.py，是独立脚本（一个 test_ 函数都没有）。
    #    pytest 按 test_*.py 命名去收集它 → 收到 0 个测试 → 跳过；
    #    而这个列表里又没它 → run_all 也不跑。
    #
    #    结果：14 条权限流程测试（降权免码、升权要码、错码拒绝、
    #    通配符拒绝、码绑具体任务）一直写在那儿，一条都没执行过。
    #
    #    而它还顺手改了 guard.send_code_email 从不恢复，
    #    污染了 pytest 全量跑时的其他测试（2026-11-02 才发现）。
    #
    #    现在改名叫 perm_verify.py（不再被 pytest 误收集），
    #    并在这里真正跑起来。
    print(f"\n{'-' * 66}")
    print("  权限验证")
    print(f"{'-' * 66}")

    pv = os.path.join(ROOT, "tests", "perm_verify.py")
    if os.path.exists(pv):
        r = subprocess.run(
            [sys.executable, pv],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        for _line in (r.stdout or "").splitlines():
            _s = _line.strip()
            if "结果：" in _s or _s.startswith("❌"):
                print(f"  {_s}")
        if r.returncode != 0:
            failed_modules.append("权限验证")

    # ── 端到端（真起进程、真派活）──
    #
    # ⚠️ 这一节是今天加的，因为前面所有测试都在**同一个进程里**跑。
    #
    #        它们测"函数返回对不对"，不测"拉下来能不能用"。
    #
    #    而 1.3.3 那次重构的两处断链恰好都在进程边界上：
    #        · bridge.py 移进 core/ 了，scheduler/api_server 还在旧路径找
    #          → worker 起不来，同进程测试一点都看不出来
    #        · core/bridge.py 用相对导入
    #          → 脚本模式静默失败 → 退回 DSH → 用错 provider
    #
    #    347 条测试全绿，功能却是瘫的。这就是"假绿"。
    #
    #    这一节不管"函数对不对"，只管"真的能跑起来吗"。
    #    它慢，但它是唯一能回答那个问题的东西。
    print(f"\n{'─' * 66}")
    print("  端到端（真起服务、真派活）")
    print(f"{'─' * 66}")

    e2e = os.path.join(ROOT, "tests", "e2e.py")
    if os.path.exists(e2e):
        try:
            r = subprocess.run(
                [sys.executable, e2e],
                cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=600,
            )
            out = r.stdout or ""
            for line in out.splitlines():
                s = line.strip()
                if s.startswith("❌") or s.startswith("⏭"):
                    print(f"  {s}")
            for line in out.splitlines():
                if "合计：" in line:
                    print(f"  {line.strip()}")

            import re as _re

            m = _re.search(r"合计：(\d+) 通过，(\d+) 失败", out)
            if m:
                e2e_pass, e2e_fail = int(m.group(1)), int(m.group(2))
                total_pass += e2e_pass
                total_fail += e2e_fail
                if e2e_fail > 0:
                    failed_modules.append(f"端到端({e2e_fail})")
                    for line in out.splitlines():
                        if line.strip().startswith("·"):
                            print(f"    {line.strip()}")
            else:
                total_fail += 1
                failed_modules.append("端到端(异常)")
                tail = (out[-600:] + (r.stderr or "")[-600:])
                for ln in tail.splitlines()[-15:]:
                    print(f"    {ln}")
        except subprocess.TimeoutExpired:
            total_fail += 1
            failed_modules.append("端到端(超时)")

    elapsed = time.time() - t0

    print(f"\n{'=' * 66}")
    print(f"  合计：{total_pass} 通过，{total_fail} 失败（{elapsed:.1f} 秒）")
    if failed_modules:
        print(f"  失败模块：{', '.join(failed_modules)}")

        # ⚠️ 给 CI 用的：打 FAILED 开头的话。
        #
        #    workflow 里那条 grep -E '^(FAILED|tests/.*FAILED)'
        #    需要这个前缀，才能把失败名做成 annotation。
        #    没有这行 → CI 红了但 annotation 是空的 → 作者不知道挂在哪。
        #
        #    2026-11-03 实测：a59e69f 在 ubuntu 上红了，
        #    annotation 只有一个「Process completed with exit code 1」，
        #    完全看不出是哪个测试。
        for _m in failed_modules:
            print(f"FAILED: {_m}")
    print("=" * 66)

    # ⚠️ 退出码要同时看 total_fail 和 failed_modules。
    #
    #    原来只有 `0 if total_fail == 0 else 1`，而 total_fail
    #    只从 pytest 输出里正则抓 "(\d+) failed" 累加。
    #
    #    brute_force.py 失败时是 failed_modules.append 的，
    #    **不参与这个判断** —— 所以哪怕暴力攻击挂了，
    #    run_all.py 照样 exit 0。
    #
    #    这条之前被掩盖了：CI 把 brute_force.py 单独列了一个 step，
    #    靠那一步的 returncode 兜住。但只跑 run_all.py 的人
    #    （比如本地开发）会看到假的"0 失败"。
    #
    #    现在两个都要过才算过。
    return 0 if (total_fail == 0 and not failed_modules) else 1


if __name__ == "__main__":
    sys.exit(main())
