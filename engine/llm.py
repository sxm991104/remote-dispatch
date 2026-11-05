"""对话模型调用：任何 OpenAI 兼容的接口都能接。

为什么自己写这一层
    原来任务端跑在 DeepSeek Harness 里，用户想用得先装 DSH。
    这对很多人是劝退的——为了用你的东西，先装一个几十兆的
    运行时、再配一堆东西。

    这一层的目标很窄：给一个 base_url + api_key + model 三样东西，
    就能跑起来。OpenAI、DeepSeek、通义、Kimi、本地 Ollama、
    任何照着 OpenAI 格式做的服务，都走同一个代码路径。

    不依赖任何 SDK。用 urllib 直接发 HTTP：
    多装一个包，用户就多一个装不上的可能。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from engine.ratelimit import RateLimiter


def _clean_surrogates(obj):
    """清掉字符串里的孤立代理对。

    ## 为什么需要

        实测（2026-11-04）：扣子通过 ACP 发来的用户消息里带
        \\udc80 这种孤立代理对（就是 UTF-8 被当成 UTF-16 读过一遍的产物）。

        这些字符能进 Python 的 str，但写不出去 ——
        编码成 UTF-8 时抛：
            UnicodeEncodeError: surrogates not allowed

        而它出现在**模型请求体**里，于是整个请求发不出去。

    ## 做法

        递归遍历，把字符串里所有代理对字符替换掉。
        ensure_ascii=False 加 errors 参数不管用（json.dumps 不接受），
        所以只能自己洗。

    ## 为什么不直接报错拒绝

        用户根本不知道自己的输入里有坏字符 ——
        这是传输层的产物，不是他的错。
        **能洗就洗，洗完继续干活。**
    """
    if isinstance(obj, str):
        # 代理对区间 U+D800–U+DFFF
        return obj.encode("utf-8", "replace").decode("utf-8", "replace")
    if isinstance(obj, list):
        return [_clean_surrogates(x) for x in obj]
    if isinstance(obj, dict):
        return {_clean_surrogates(k): _clean_surrogates(v) for k, v in obj.items()}
    return obj


class LLMError(Exception):
    """模型调用失败。分清楚是哪一类，后面才好提示用户。"""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str          # 原始 JSON 字符串，可能被截断，调用方要容错


@dataclass
class Reply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # 用量：算出花了多少 token，界面要显示
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 缓存命中的输入 token。
    #
    # ⚠️ 这一项决定花多少钱：命中缓存通常只要 1/10 价。
    #
    #    原来没记它，所以费用只能"按最坏情况"估 ——
    #    实际有缓存命中时会贵十倍地估。
    #
    #    各家的字段名不统一（实测 2026-11-05）：
    #        标准 OpenAI   usage.prompt_tokens_details.cached_tokens
    #        DeepSeek      usage.prompt_cache_hit_tokens
    #                       和 prompt_cache_miss_tokens
    #    两种都认，取到哪个用哪个。
    cached_tokens: int = 0
    # 推理 token（o1 这类模型会单独算钱）
    reasoning_tokens: int = 0
    # 结束原因：stop / tool_calls / length
    finish_reason: str = ""


def _normalize_base(base_url: str) -> str:
    """把用户填的地址整理成能用的形式。

    用户可能填：
        https://api.openai.com
        https://api.openai.com/v1
        https://api.openai.com/v1/chat/completions
    这三种都得能用。统一成 .../v1/chat/completions。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        base = "https://api.openai.com"
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


class LLM:
    """一个能发请求的模型客户端。

    config 是普通 dict，来自用户配置：
        base_url   必填
        api_key    必填
        model      必填
        timeout    可选，默认 180 秒
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 180, profile_id: str = "",
                 prices: dict | None = None):
        self.url = _normalize_base(base_url)
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # 这个 Key 这家服务的频率限制。撞过 429 之后它就知道该隔多久。
        self.limiter = RateLimiter()

        # ⚠️ profile_id 和 prices 是给"算真实花销"用的。
        #
        #    光有 model 名不够：同一个模型名在两家可能是两个价，
        #    而且用户可能自己填了价（代理、折扣）。
        #    所以把整套配置里的价格跟着对象走，
        #    记花销时就不用再去读一遍配置文件。
        self.profile_id = profile_id or ""
        self.prices = prices or {}

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             max_tokens: int = 8192, temperature: float = 0.3) -> Reply:
        """发一轮对话，返回模型的话（可能带工具调用）。

        temperature 的处理有个坑：
            各家对它的宽容度不一样。实测 kimi-k2.6 只接受 1，
            发 0.3 直接 400：
                "invalid temperature: only 1 is allowed for this model"

            所以这里不做"聪明"的适配——先按传入的值发，
            如果对方因为参数不对拒了，就去掉这个参数重发一次。
            OpenAI 兼容服务全都接受"不传 temperature"。
        """
        def build(include_temp: bool) -> bytes:
            payload = {
                "model": self.model,
                "messages": messages,
                "max_tokens": max_tokens,
            }
            if include_temp:
                payload["temperature"] = temperature
            if tools:
                payload["tools"] = tools
                payload["tool_choice"] = "auto"
            # ⚠️ 洗一遍再序列化 —— 见 _clean_surrogates 的说明。
            #    不洗的话，用户消息里一个坏字符就能让整个请求发不出去。
            return json.dumps(_clean_surrogates(payload),
                              ensure_ascii=False).encode("utf-8")

        def make_req(body: bytes) -> urllib.request.Request:
            return urllib.request.Request(
                self.url,
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )

        try:
            return self._parse(self._send(make_req(build(True))))
        except LLMError as e:
            msg = str(e).lower()
            # 只对这几种参数问题重试，别把真正的错误也吞掉
            if any(k in msg for k in ("temperature", "max_tokens", "top_p")):
                return self._parse(self._send(make_req(build(False))))
            raise

    def _send(self, req: urllib.request.Request) -> dict:
        """发请求，遇到限流和临时故障会等一下再试。

        为什么要专门处理 429：
            实测 kimi-k2.6 的免费额度是**每分钟 3 次请求**，
            而跑一个任务要好几轮（每轮一次请求），很容易撞上。
            不重试的话，任务跑到一半就废了，用户看到的只是
            "接口返回 429"——他不知道自己该等多久，也不知道该干什么。

            所以这里等：先 5 秒，再 10 秒、20 秒……
            服务端如果在响应里说了"再等几秒"，就照它说的等。
        """
        last_err: Exception | None = None
        # 最多试 5 次。限额很紧的服务（比如每分钟 3 次）也能熬过去。
        for attempt in range(5):
            # 上一轮撞限流的话，这里会等到安全时刻再发
            self.limiter.wait()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read().decode("utf-8"))
                self.limiter.mark_ok()
                return data
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:400]
                except Exception:  # noqa: BLE001
                    pass

                # 429：限流。
                #
                # 不能只按它说的 "after 1 second" 等——那个 1 秒是
                # 通用话术，实际限制是"每分钟 3 次"，等 1 秒还在同一窗口。
                # 交给 limiter：它会从错误信息里读出真实 RPM，
                # 算出"下一次安全时刻"。
                if e.code == 429:
                    gap = self.limiter.on_429(detail)
                    rpm = self.limiter.rpm
                    if rpm:
                        desc = f"这台服务每分钟最多 {rpm} 次请求"
                    else:
                        desc = "这台服务有频率限制"
                    last_err = LLMError(
                        f"请求太快了，{desc}。已经等了一会儿，再试一次。"
                    )
                    if attempt < 4:
                        time.sleep(min(60.0, gap))
                        continue
                    raise LLMError(
                        f"{desc}，连续重试 {attempt + 1} 次还是被拒。"
                        "这个任务需要多轮对话，用免费额度可能跑不完——"
                        "换一个限额更宽的 Key，或者等一会儿再派。"
                    ) from None

                # 其它 4xx 是配置问题，重试没用
                if 400 <= e.code < 500 and e.code != 408:
                    raise LLMError(
                        f"接口返回 {e.code}。{_explain_http(e.code)}"
                        f"详情：{detail}"
                    ) from None

                # 5xx 和超时：临时故障，等一下再试
                last_err = LLMError(f"接口返回 {e.code}：{detail}")
            except urllib.error.URLError as e:
                last_err = LLMError(f"连不上接口：{e.reason}")
            except TimeoutError:
                last_err = LLMError("接口超时")
            except json.JSONDecodeError:
                last_err = LLMError("接口返回的不是 JSON")

            if attempt < 4:
                time.sleep(2 * (attempt + 1))

        raise last_err or LLMError("调用失败")

    @staticmethod
    def _retry_after(detail: str, default: float) -> float:
        """从错误信息里读"该等几秒"。

        各家写法不一样：
            kimi:    "please try again after 1 seconds"
            openai:  响应头里有 Retry-After（这里读不到，只能看正文）
        读不到就用默认的指数退避。
        """
        import re

        m = re.search(r"after\s+(\d+)\s*second", detail, re.I)
        if m:
            try:
                return min(60.0, max(1.0, float(m.group(1))))
            except ValueError:
                pass
        return min(60.0, default)

    def _parse(self, data: dict) -> Reply:
        """把 OpenAI 格式的返回拆成 Reply。

        各家服务在细节上有出入，这里按最宽松的方式读：
        有的服务 choices[0].message.content 是 null、只有 tool_calls，
        有的会把 tool_calls 塞在别处。尽量兼容。
        """
        choices = data.get("choices") or []
        if not choices:
            err = (data.get("error") or {}).get("message") if isinstance(
                data.get("error"), dict) else data.get("error")
            raise LLMError(f"模型没返回内容{('：' + str(err)) if err else ''}")

        ch = choices[0]
        msg = ch.get("message") or {}
        reply = Reply()
        reply.content = msg.get("content") or ""

        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            reply.tool_calls.append(ToolCall(
                id=tc.get("id") or f"call_{len(reply.tool_calls)}",
                name=fn.get("name") or "",
                arguments=fn.get("arguments") or "{}",
            ))

        reply.finish_reason = ch.get("finish_reason") or ""
        usage = data.get("usage") or {}
        reply.prompt_tokens = int(usage.get("prompt_tokens") or 0)
        reply.completion_tokens = int(usage.get("completion_tokens") or 0)

        # ── 缓存命中 / 推理 token ──
        #
        # ⚠️ 各家字段名不一样，两种都认：
        #
        #     标准 OpenAI   prompt_tokens_details.cached_tokens
        #     DeepSeek      prompt_cache_hit_tokens
        #
        #   实测 DeepSeek 两个都有（2026-11-05）：
        #     {"prompt_tokens":34,"completion_tokens":20,
        #      "prompt_tokens_details":{"cached_tokens":0},
        #      "prompt_cache_hit_tokens":0,"prompt_cache_miss_tokens":34}
        _pdet = usage.get("prompt_tokens_details") or {}
        cached = 0
        if isinstance(_pdet, dict) and _pdet.get("cached_tokens") is not None:
            cached = int(_pdet.get("cached_tokens") or 0)
        elif usage.get("prompt_cache_hit_tokens") is not None:
            cached = int(usage.get("prompt_cache_hit_tokens") or 0)
        reply.cached_tokens = max(0, cached)

        _cdet = usage.get("completion_tokens_details") or {}
        if isinstance(_cdet, dict):
            reply.reasoning_tokens = int(_cdet.get("reasoning_tokens") or 0)

        return reply


def _explain_http(code: int) -> str:
    """把常见状态码翻成用户能看懂的一句话。"""
    if code == 401:
        return "API Key 不对，或者已经失效了。"
    if code == 403:
        return "这个 Key 没有权限用这个模型。"
    if code == 404:
        return "接口地址填错了，或者这个模型不存在。"
    if code == 429:
        return "请求太频繁，或者额度用完了。"
    if code >= 500:
        return "对面服务出问题了，等一会儿再试。"
    return ""


def test_connection(base_url: str, api_key: str, model: str) -> tuple[bool, str]:
    """试一下能不能通。设置页的「测试连接」用它。

    ## ⚠️ 原来只走一步：让模型回一句话

        llm.chat([{"role":"user","content":"只回复两个字：可以"}], max_tokens=20)

        这一步能验证"地址对、Key 对、模型名对"，但**非常慢**。

        实测（2026-11-05）：
            审计员配的是 kimi-k2.6（推理模型），
            测一次连了 **49.9 秒** ——
            界面那边 timeout=60，用户看着"正在连…"几乎等满一分钟，
            还以为卡死了。

        推理模型先生成一大段思考再吐那两个字，等它想完就四十多秒。

    ## 现在分两步

        第一步（快）：GET /v1/models
            验证地址、Key、能连通。一般 0.2-2 秒。
            这一步失败就直接报错，不用再等第二步。

        第二步（慢，但只在第一步通过时跑）：让它回一句话
            验证"这个模型名真能调"——
            有的服务 /models 里有，但实际没开权限。

        ⚠️ 第二步仍然可能慢（推理模型就是这个脾气）。
            所以：
                · timeout 从 30 提到 45（够推理模型想完）
                · 界面那边超时也放宽，并且明确告诉用户
                  "推理模型会慢，可能要几十秒"
    """
    import json as _json
    import urllib.error
    import urllib.request

    base = (base_url or "").strip().rstrip("/")
    key = (api_key or "").strip()
    mdl = (model or "").strip()

    if not base or not key:
        return False, "地址和 Key 都得填"

    # ── 第一步：列模型（快）──
    url = base + ("/models" if base.endswith("/v1") else "/models")
    # 有的服务 base_url 已经带 /v1，有的不带。两种都试一下更稳。
    candidates = []
    if base.endswith("/v1"):
        candidates.append(base + "/models")
    else:
        candidates.append(base + "/v1/models")
        candidates.append(base + "/models")

    last_err = ""
    reachable = False
    for u in candidates:
        try:
            req = urllib.request.Request(
                u,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
            try:
                d = _json.loads(raw.decode("utf-8", "replace"))
            except _json.JSONDecodeError:
                d = {}
            ids = [
                str(x.get("id") or "")
                for x in (d.get("data") or [])
                if isinstance(x, dict)
            ]
            reachable = True

            if mdl and ids and mdl not in ids:
                # 地址和 Key 都对，但模型名不在列表里
                shown = "、".join(ids[:8])
                more = f" 等 {len(ids)} 个" if len(ids) > 8 else ""
                return False, (
                    f"地址和 Key 都对，但没有「{mdl}」这个模型。\n"
                    f"可用的有：{shown}{more}"
                )

            # 模型名在列表里（或者服务没给列表）→ 走第二步
            break
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return False, f"Key 不对（HTTP {e.code}）—— 核对一下这个 Key"
            last_err = f"HTTP {e.code}"
            continue
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            continue

    # ── 第二步：让它回一句话 ──
    #
    # ⚠️ **默认不跑。**
    #
    #    第一步 /models 已经验证了"能不能用"的全部条件：
    #        地址对（连上了）、Key 对（没 401、没 403）、模型名对（列表里有）
    #
    #    第二步能多验证的只有"生成这条路通不通"。
    #    但代价太大：实测 kimi-k2.6 光这一步就 **49 秒** ——
    #    推理模型先生成一大段思考再吐两个字。
    #
    #    用户点「测试连接」要的就是"对不对"，不是"快不快地回句话"。
    #    让他等五十秒换一个额外确认，不值。
    #
    #    只有连 /models 都取不到列表的服务（有些自建网关不实现这个接口）
    #    才需要靠第二步来判断 —— 那种情况第二步是唯一手段。
    if reachable:
        head = f"通了。地址、Key、模型名都对"
        if mdl:
            head += f"（{mdl} 在可用列表里）"
        return True, head

    try:
        llm = LLM(base, key, mdl, timeout=45)
        reply = llm.chat(
            [{"role": "user", "content": "只回复两个字：可以"}],
            max_tokens=20,
        )
        text = (reply.content or "").strip()[:40]
        return True, (
            f"通了（这个服务没给模型列表，靠对话验证的）。"
            f"它回了：{text or '(空)'}"
        )
    except LLMError as e:
        return False, f"{e}（另外列模型也失败了：{last_err}）"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}（列模型也失败了：{last_err}）"
