"""发一封普通的信。

## 为什么单独一个文件

    这是今天加的。原来写在 guard/guard.py 里（84 行），
    而 guard.py 已经 1260 行了。

    它跟 guard.py 里那些东西（拦截、锁死、验证码）没有关系 ——
    它只做一件事：把一段文字发到用户邮箱。

    放在 guard.py 里，是把两个不同的关注点搅在一起：
        guard.py     "要不要拦"的逻辑
        这里         "怎么发出去"的管道

## 谁在用

    guard/auditor.py 的审核员 —— 它有自己的码（guard/auditor_code.py），
    需要一条纯粹的"把这段文字发出去"的通道。

## ⚠️ 跟 guard.send_code_email 的区别

    send_code_email
        自己生成码，而且**故意不返回码**
        （"返回给调用方就等于写进了对话上下文，那这个码就废了"）
        用于任务端的危险操作确认

    send_raw_email（这里）
        只发，不管内容。码由调用方自己生成和保管。

    ⚠️ 调用方拿到的码**绝对不能** print、写日志、或者放进任何
       会回到前台的东西里。进了上下文/日志就等于泄露。

## 配置

    读的就是 guard.json 里的 smtp_* / email 那几个字段 ——
    跟 guard.send_code_email 同一份配置。
    用户只填一次邮箱，两条路都能用。
"""

from __future__ import annotations

import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr


def send(subject: str, body: str,
         config: dict | None = None) -> tuple[bool, str]:
    """发一封纯文本邮件。返回 (成功, 提示)。

    config：传了就用它，不传就读 guard 已保存的配置。

        ⚠️ 为什么要这个参数（跟 send_code_email 同样的理由）：
            "填 → 测 → 存" 是正常顺序。用户填完就想先测一发。
            但一进来就 load_config()，读的是**上一次保存的**值。
            结果：用户填了 163 邮箱，点「发封测试邮件」，
            实际还在连旧的 —— 必然失败，而他以为是自己填错了。
    """
    # ⚠️ **静默期在"发信"这一层也要拦一道。**
    #
    #    原来只在各自的 ensure_code() 里判静默 ——
    #    那只管"要不要生成新码"。但发信这一步是分开的：
    #
    #        码可能早就生成好了（比如发码前一刻开的静默），
    #        或者调用方手里拿着一个现成的码直接来发信。
    #
    #    这种情况下，ensure_code 那道判断**已经走过了**，
    #    静默期间照样会把信发出去。
    #
    #    实测（2026-11-05）：老爸问"有没有漏掉发验证码的地方"，
    #    扫出来两条走 emailer 的路
    #    （auditor.py 审核端码、netauth.py 联网码），
    #    它们只在生成码那层判了静默。
    #
    #    现在在最底层的发信口再判一次 —— 漏不掉。
    try:
        from guard import quiet as _q

        if _q.is_quiet():
            return True, (
                f"现在是静默期（还有 {_q.remaining() // 60} 分钟），不发验证码。"
            )
    except Exception:  # noqa: BLE001
        pass

    # 延迟导入：guard 是重模块，这里不该在 import 期就拉起来
    if config is None:
        from guard import guard as _g

        config = _g.load_config()

    c = config
    if not (c.get("email") and c.get("smtp_host")):
        return False, "还没配安全邮箱（见设置页「远程验证」）"

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr(
            ("电脑端", c.get("smtp_user") or c.get("email"))
        )
        msg["To"] = c["email"]

        host = c.get("smtp_host")
        port = int(c.get("smtp_port") or 465)

        # 端口决定用不用 SSL。
        #
        # ⚠️ 不能靠 smtp_ssl 那个标记 ——
        #    全仓没有一处写它，永远是默认值，等于恒真。
        #    而预置表里 outlook 之类是 587，必须 STARTTLS。
        #
        #     465        → SSL
        #     587 / 25   → 明文连接 + STARTTLS
        #     其他       → 有显式 smtp_ssl 就听它，没有就按明文走
        explicit = c.get("smtp_ssl")
        if port == 465:
            use_ssl = True
        elif port in (587, 25, 2525):
            use_ssl = False if explicit is None else bool(explicit)
        else:
            use_ssl = bool(explicit) if explicit is not None else False

        if use_ssl:
            server = smtplib.SMTP_SSL(
                host, port, timeout=20,
                context=ssl.create_default_context(),
            )
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()

        try:
            if c.get("smtp_user") and c.get("smtp_pass"):
                server.login(c["smtp_user"], c["smtp_pass"])
            server.sendmail(msg["From"], [c["email"]], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass

        return True, f"已发出"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
