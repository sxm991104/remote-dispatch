# -*- coding: utf-8 -*-
"""写文件走影子层 —— 钉住"防护不再挂在进程上"。

## 背景

    原来 write_file / edit_file 是在 Python 进程里 open().write()：

        写文件的进程在沙盒**外面** → Sandboxie 按进程隔离，包不住
            ↓
        classify_changes 抓不到 → auditor.review 收不到 diff
            ↓
        **两道闸全绕过，只剩路径检查**

    而危险不在"写了什么"，在"这个文件本身能执行"：
    开机自启目录、.git/hooks、计划任务 XML、guard/guard.json。

    （2026-11-06 辩论里被指出来：防护挂在进程上、危险挂在文件上。）

## 这里的测试钉三件事

    一、写入确实经过影子层（真机在审核回写之前不动）
    二、内容原样落地（中文 / 引号 / 换行 / 反斜杠 / %变量%）
    三、自己的临时文件不能污染结果判断（那个坑踩过）
"""
import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from engine import tools  # noqa: E402
from guard import sandbox  # noqa: E402


def box_ok() -> bool:
    try:
        return bool(sandbox.available())
    except Exception:  # noqa: BLE001
        return False


needs_box = pytest.mark.skipif(
    not box_ok(), reason="这台机器没装 Sandboxie，影子层跑不了"
)


@pytest.fixture()
def wd(tmp_path):
    """一个干净的工作目录，测完自动清。"""
    old = tools.workdir()
    tools.set_workdir(str(tmp_path))
    try:
        if box_ok():
            sandbox.clear_box()
        yield str(tmp_path)
    finally:
        try:
            if box_ok():
                sandbox.clear_box()
        except Exception:  # noqa: BLE001
            pass
        tools.set_workdir(old)


# ══════════════════════════════════════════════════════
# 一、内容原样落地
# ══════════════════════════════════════════════════════

class TestContentRoundTrip:
    def test_plain(self, wd):
        r = tools.write_file("a.txt", "hello\n")
        assert "已写入" in r
        assert open(os.path.join(wd, "a.txt"), encoding="utf-8").read() == "hello\n"

    @needs_box
    def test_chinese_quotes_newlines(self, wd):
        """⚠️ 内容要塞进 cmd 再塞进 .bat，每一层都能弄坏它。

        实测：中文、引号、换行、反斜杠、%变量%、$变量%
        这几样各自都会踩不同的坑。
        """
        content = '第一行 "引号"\n第二行 C:\\Windows\\System32\n第三行 %TEMP% $HOME\n'
        r = tools.write_file("b.txt", content)
        assert "已写入" in r, r
        got = open(os.path.join(wd, "b.txt"), encoding="utf-8").read()
        assert got == content, f"内容被改了：{got!r}"

    def test_empty_content(self, wd):
        tools.write_file("empty.txt", "")
        assert os.path.exists(os.path.join(wd, "empty.txt"))

    def test_nested_dir_created(self, wd):
        tools.write_file("x/y/z.txt", "deep")
        assert os.path.exists(os.path.join(wd, "x", "y", "z.txt"))

    def test_edit_file(self, wd):
        tools.write_file("c.txt", "one two three\n")
        r = tools.edit_file("c.txt", "two", "TWO")
        assert "已改" in r
        assert open(os.path.join(wd, "c.txt"), encoding="utf-8").read() == "one TWO three\n"


# ══════════════════════════════════════════════════════
# 二、确实经过影子层
# ══════════════════════════════════════════════════════

class TestGoesThroughBox:
    @needs_box
    def test_write_in_box_lands_in_shadow_not_real(self, wd, tmp_path):
        """核心断言：沙盒写完后，**真机文件不该变**。

        ⚠️ 这一条是整件事的关键。
           如果沙盒里的写入直接改了真机，
           说明隔离没生效，后面审核回写全白做。
        """
        target = os.path.join(wd, "probe.txt")
        with open(target, "w", encoding="utf-8") as f:
            f.write("OLD\n")

        sandbox.clear_box()
        base = sandbox.snapshot()

        ok, why = sandbox.write_in_box(target, "NEW\n")
        assert ok, f"写影子失败：{why}"

        # 真机还没变
        assert open(target, encoding="utf-8").read() == "OLD\n", (
            "沙盒里的写入直接落到了真机 —— 隔离没生效"
        )

        # 影子层里抓得到
        ch = sandbox.classify_changes(base)
        n = len(ch.get("writes") or []) + len(ch.get("modifies") or [])
        assert n >= 1, f"classify_changes 什么都没抓到：{ch}"

    @needs_box
    def test_apply_writes_it_back(self, wd):
        """审核放行之后，真机才应该有新内容。"""
        target = os.path.join(wd, "back.txt")
        sandbox.clear_box()
        base = sandbox.snapshot()

        ok, _ = sandbox.write_in_box(target, "FROM BOX\n")
        assert ok

        import time
        time.sleep(0.4)
        res = sandbox.apply_after_run(baseline=base)

        recovered = [os.path.normcase(str(x)) for x in (res.get("recovered") or [])]
        assert os.path.normcase(target) in recovered, (
            f"目标没回写：recovered={res.get('recovered')} denied={res.get('denied')}"
        )
        assert open(target, encoding="utf-8").read() == "FROM BOX\n"


# ══════════════════════════════════════════════════════
# 三、自己的临时文件不能污染判断
# ══════════════════════════════════════════════════════

class TestOwnTempFilesDontPollute:
    """⚠️ 这个坑真踩过，而且很难看出来。

    为了在沙盒里跑写入，代码会往 TEMP 放一个 .py 给沙盒用。
    那个文件也算影子里的新建 —— 回写时被判「workspace 权限范围外」，
    于是进了 denied。

    而当时判断"写没写成"用的是 `denied or not recovered` ——
    目标明明写成功了，却被自己的临时文件误报成失败。
    """

    @needs_box
    def test_success_not_misreported(self, wd):
        r = tools.write_file("ok.txt", "content\n")
        assert "已写入" in r, f"被自己的临时文件误报了：{r}"
        assert "没落到真机" not in r

    @needs_box
    def test_target_really_has_content(self, wd):
        """返回说写成了，文件就得真的有内容 —— 不能只是"没报错"。"""
        tools.write_file("ok2.txt", "验证一下\n")
        p = os.path.join(wd, "ok2.txt")
        assert os.path.exists(p)
        assert open(p, encoding="utf-8").read() == "验证一下\n"


# ══════════════════════════════════════════════════════
# 四、路径检查还在（影子层不能取代它）
# ══════════════════════════════════════════════════════

class TestPathCheckStillFirst:
    def test_outside_workdir_raises(self, wd):
        """工作区外直接抛 PermissionError。

        ⚠️ 顺序：先路径检查，再影子层。
           反过来的话，越界文件会先进影子层，
           白白跑一趟沙盒。
        """
        with pytest.raises(PermissionError):
            tools.write_file(r"C:\Windows\System32\_nope.txt", "x")

    def test_relative_ok(self, wd):
        tools.write_file("rel.txt", "ok")
        assert os.path.exists(os.path.join(wd, "rel.txt"))
