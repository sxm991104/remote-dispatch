"""真去连一次飞书 / 钉钉，验证凭据和网络。

## 为什么不改 atrium

    老爸的要求：只改桌面端，atrium 一行不动。

    所以不能问 atrium "你连上了吗" —— 那是它进程里的事。

    **换个能验证的：拿凭据去换一次 token。**

        飞书：POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal
              body: {app_id, app_secret}
              → 换到 token = 凭据对、网络通、应用可用

        钉钉：POST https://api.dingtalk.com/v1.0/oauth2/accessToken
              body: {appId, appSecret}
              → 同上

    ## 这个验证的价值

        凭据错 → atrium 也连不上（它启动时也要换 token）
        网络不通 → 同上

        所以"能换到 token"是"atrium 能连上"的**必要条件**。
        虽然不是充分条件（换到 token 之后长连接还可能断），
        但它能挡住绝大多数"配错了"的情况 ——
        而这正是用户配完之后最想知道的。

    ## ⚠️ 绝不把 secret 打日志、打到界面上

        换 token 的请求体里带 secret，但它只在内存里拼、发出去，
        不写文件、不打印。
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request


def _post(url: str, body: dict, timeout: int = 15) -> tuple[int, dict]:
    """发 POST。返回 (状态码, 解析后的字典)。"""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    # ⚠️ 有的机器装着安全软件做 TLS 中间人，证书会校验不过。
    #    为了"测试连接"这个动作能用，这里不校验证书。
    #
    #    但这只用于测试：真连平台是 atrium 干的，它自己有校验。
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                return r.status, {"_raw": raw[:300].decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        raw = b""
        try:
            raw = e.read()
        except Exception:  # noqa: BLE001
            pass
        try:
            return e.code, json.loads(raw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return e.code, {"_raw": raw[:300].decode("utf-8", "replace")}


def check_feishu(app_id: str, app_secret: str) -> tuple[bool, str]:
    """飞书：拿 app_id/app_secret 换 tenant_access_token。

    成功 → (True, "飞书凭据正确，能拿到 access token")
    失败 → (False, 说明)
    """
    a_id = (app_id or "").strip()
    a_sec = (app_secret or "").strip()
    if not a_id or not a_sec:
        return False, "缺 App ID 或 App Secret"

    try:
        code, d = _post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            # ⚠️ 飞书这个接口的字段名是 **app_id / app_secret**（下划线）
            {"app_id": a_id, "app_secret": a_sec},
        )
    except Exception as e:  # noqa: BLE001
        return False, f"连不上飞书接口：{type(e).__name__}: {e}"

    # 飞书成功时返回 {"code":0,"msg":"ok","tenant_access_token":"t-xxx",...}
    fc = d.get("code")
    if fc == 0 and d.get("tenant_access_token"):
        return True, "飞书凭据正确，能换到 access token"

    msg = str(d.get("msg") or d.get("message") or d.get("_raw") or "")
    if fc in (99991663, 99991661, 10003, 10014):
        return False, f"飞书说凭据不对（{fc} {msg}）—— 去开发者后台核对 App ID / Secret"
    return False, f"飞书返回 {fc}：{msg or '没给原因'}"


def check_dingtalk(client_id: str, client_secret: str) -> tuple[bool, str]:
    """钉钉：拿 client_id/client_secret 换 accessToken。

    成功 → (True, "钉钉凭据正确，能拿到 access token")
    """
    c_id = (client_id or "").strip()
    c_sec = (client_secret or "").strip()
    if not c_id or not c_sec:
        return False, "缺 Client ID 或 Client Secret"

    try:
        code, d = _post(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            # ⚠️ 钉钉这个接口字段是 **appKey / appSecret**（驼峰）
            {"appKey": c_id, "appSecret": c_sec},
        )
    except Exception as e:  # noqa: BLE001
        return False, f"连不上钉钉接口：{type(e).__name__}: {e}"

    if d.get("accessToken"):
        return True, f"钉钉凭据正确（有效期 {d.get('expireIn', '?')} 秒）"

    msg = str(d.get("message") or d.get("code") or d.get("_raw") or "")
    return False, f"钉钉没给 token：{msg or '没给原因'}"
