"""外传扫描：内容里有没有"要往外传"的痕迹。

## 这不是"扫机密"，是"扫外传意图"

    用户的机密长什么样，我们不知道，也猜不准。
    但"要把东西发出去"这件事，是有形状的：

        别人的邮箱地址     xxx@someone-else.com
        别人的网址         http://evil.com/upload
        网盘链接            pan.baidu.com / drive.google.com
        webhook            discord.com/api/webhooks / hooks.slack.com
        发送指令           "发送到" "上传到" "发到 xxx"
        FTP / 远程复制      ftp:// / scp xxx@ / rclone copy

    这些**能做得准**，而且误报率低。

## ⚠️ 不做什么

    不扫"密钥格式"（-----BEGIN / sk- / AKIA）。
    为什么：用户可能就是要发一个密钥给同事，那是他的权利。
    我们要拦的是"偷偷往外发"，不是"发敏感内容"。

    （密钥那类东西，沙箱断网已经挡住了——它跑不出去。）

## ⚠️ 扫描失败怎么办

    扫描本身出错 → **按"没扫到"处理，放行**。
    理由：这是个辅助检查，不能因为它挂了就把功能卡死。
    但它**不能抛异常** —— 那样调用方就崩了。
"""

from __future__ import annotations

import re

# 用户自己的邮箱 —— 扫描时要排除掉。
# 由调用方传进来（从 guard.json 里读）。

# ── 别人的邮箱 ──
RE_EMAIL = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}"
)

# ── 上传/接收服务的 URL ──
# 这些域名一出现，基本就是要往外送东西
UPLOAD_HOSTS = (
    # 网盘
    "pan.baidu.com", "drive.google.com", "dropbox.com",
    "pan.xunlei.com", "aliyundrive.com", "alipan.com",
    "115.com", "weiyun.com", "onedrive.live.com", "1drv.ms",
    # 临时文件分享
    "transfer.sh", "file.io", "0x0.st", "gofile.io",
    "catbox.moe", "litterbox.catbox.moe", "anonfiles",
    "wetransfer.com", "send.firefox.com",
    # 代码/内容托管（可以贴东西进去）
    "pastebin.com", "hastebin.com", "ghostbin.com",
    "gist.github.com", "paste.ee", "dpaste.org",
    # webhook
    "hooks.slack.com", "discord.com/api/webhooks",
    "oapi.dingtalk.com/robot", "qyapi.weixin.qq.com/cgi-bin/webhook",
    # 图片/文件上传
    "imgur.com", "imgbb.com", "sm.ms", "cloudflare.com/imagedelivery",
)

# ── 明确的外传协议 ──
RE_PROTO = re.compile(
    r"\b(ftp|sftp|scp|rsync)://|"
    r"\bscp\s+\S+\s+\S+@|"
    r"\brclone\s+(copy|sync|move)\b",
    re.IGNORECASE,
)

# ── 中文"往外发"的说法 ──
RE_CN_SEND = re.compile(
    r"(发送|上传|传|发|同步|备份|转发)到\s*"
    r"([^\s，。；;,]{2,40})",
)

# 这些词跟在"发送到"后面时是正常的（不是外传）
CN_BENIGN_TARGETS = (
    "文件", "本地", "磁盘", "桌面", "目录", "文件夹",
    "这里", "上面", "下面", "列表", "终端", "屏幕",
    "你", "我", "用户", "电脑", "工作区",
)

# ── 英文"往外发"的说法 ──
RE_EN_SEND = re.compile(
    r"\b(upload|send|post|push|sync|export|backup)\s+"
    r"(?:it\s+|them\s+|the\s+file\s+|the\s+data\s+|everything\s+)?"
    r"(?:to|into)\s+(\S+)",
    re.IGNORECASE,
)


def scan(text: str, own_emails: list[str] | None = None,
         own_hosts: list[str] | None = None) -> list[dict]:
    """扫一遍，返回命中的问题。

    参数
        text        要扫的文本
        own_emails  用户自己的邮箱（这些不算外传目标）
        own_hosts   用户自己的域名（同上）

    返回
        [{"kind": "email", "hit": "x@y.com", "why": "..."}]
        空列表 = 没发现问题
    """
    if not text:
        return []

    own_emails = [e.lower().strip() for e in (own_emails or []) if e]
    own_hosts = [h.lower().strip() for h in (own_hosts or []) if h]
    low = text.lower()

    hits: list[dict] = []

    # ── 一、别人的邮箱 ──
    for m in RE_EMAIL.finditer(text):
        addr = m.group(0)
        if addr.lower() in own_emails:
            continue
        # 排除一些明显不是邮箱的（比如代码里的 @decorator）
        if not re.search(r"\.[a-zA-Z]{2,}$", addr):
            continue
        hits.append({
            "kind": "email",
            "hit": addr,
            "why": "内容里有一个不是你自己的邮箱地址",
        })

    # ── 二、上传/分享服务的地址 ──
    for host in UPLOAD_HOSTS:
        if host in low:
            hits.append({
                "kind": "upload_host",
                "hit": host,
                "why": "内容里提到了一个【用来把文件送出去】的服务",
            })

    # ── 三、明确的外传协议 ──
    for m in RE_PROTO.finditer(text):
        hits.append({
            "kind": "protocol",
            "hit": m.group(0)[:40],
            "why": "内容里有一条【往外传文件】的命令",
        })

    # ── 四、中文"发送到 X" ──
    for m in RE_CN_SEND.finditer(text):
        target = (m.group(2) or "").strip()
        if not target:
            continue
        if any(b in target for b in CN_BENIGN_TARGETS):
            continue
        # 目标里带域名/邮箱，更像真外传
        suspicious = (
            "@" in target
            or "." in target
            or re.search(r"(邮箱|网盘|云盘|服务器|网址|链接)", target)
        )
        if not suspicious:
            continue
        if any(h in target.lower() for h in own_hosts):
            continue
        hits.append({
            "kind": "cn_send",
            "hit": m.group(0)[:50],
            "why": "内容里有一句【把东西发到某个地方】的话",
        })

    # ── 五、英文 upload/send to ──
    for m in RE_EN_SEND.finditer(text):
        target = (m.group(2) or "").strip()
        if not target:
            continue
        if any(h in target.lower() for h in own_hosts):
            continue
        # 目标是本地路径的，不算
        if target.startswith((".", "/", "\\")) or ":" in target[:3]:
            continue
        hits.append({
            "kind": "en_send",
            "hit": m.group(0)[:50],
            "why": "内容里有一句【把东西发到某个地方】的话",
        })

    # 去重（同一个 hit 只报一次）
    seen = set()
    out = []
    for h in hits:
        k = (h["kind"], h["hit"])
        if k in seen:
            continue
        seen.add(k)
        out.append(h)

    return out


def describe(hits: list[dict]) -> str:
    """把命中结果整理成一句给人看的话。"""
    if not hits:
        return ""
    lines = ["这次要送出去的东西里，有往外的痕迹："]
    for h in hits[:8]:
        lines.append(f"  · {h['hit']}  —— {h['why']}")
    if len(hits) > 8:
        lines.append(f"  ……还有 {len(hits) - 8} 处")
    return "\n".join(lines)
