"""华为 Portal（``portal.do`` / ``webauth.do``）—— 华为 AC 原生的 Web 认证。

国内相当一部分高校的门户是华为 AC（Agile Controller / 内置 Portal）那一套，
页面与提交接口长这样：

* 未认证时，网关把 HTTP 请求 302 到
  ``http://<portal>:<port>/portal.do?wlanacip=..&wlanacname=..&wlanuserip=..&mac=..&vlan=..``
* 那个页面是一张**隐藏表单**（几十个 ``<input>``），账号密码框预置为空，
  字段名通常是 ``userId`` / ``passwd``，另外还挂一个 ``urlParameter`` 隐藏字段
  （里面就是网关那串 query）。
* 填好后 POST 到
  ``http://<portal>:<port>/webauth.do;<JSESSIONID>?<urlParameter>``

三个必须踩对的地方：

1. **覆盖，不是追加。** 表单里本来就有 ``userId`` / ``passwd`` 这两个空字段，
   再往后追加一遍会出现两个同名参数，而门户用 ``getParameter()`` 只取第一个 ——
   拿到空值，然后报「账号不存在」。所以这里是把原字段**改值**。
2. **会话号在 URL 里，不在 Cookie。** 页面里形如 ``;JSESSIONID-XXX=YYY``
   的那串要原样拼到 ``/webauth.do`` 后面，否则服务端认为是一个全新会话，
   只会把登录页原样吐回来。
3. **同账号只能一台设备在线。** 第一次提交往往只是把别的设备踢下线，
   本机得再提交一次才真正上线，所以这里最多提交两次。
"""

from __future__ import annotations

import re
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

from .base import DetectContext, LoginResult, Provider

#: 触发地址 —— 未认证时网关会把这些请求 302 到门户页面
TRIGGERS: Tuple[str, ...] = (
    "http://connect.rom.miui.com/generate_204",
    "http://edge.microsoft.com/captiveportal/generate_204",
    "http://www.msftconnecttest.com/connecttest.txt",
    "http://www.baidu.com/",
)

#: 账号 / 密码字段的可能名字（小写比较，按优先级）
USERNAME_FIELDS = ("userid", "username", "useraccount", "account", "loginname", "ddddd")
PASSWORD_FIELDS = ("passwd", "password", "pass", "upass", "pwd")

_ALREADY_HINTS = ("已在线", "已经在线", "请勿重复", "重复认证", "重复上线", "already online")
_HARD_FAILURES = (
    "密码错误", "密码不正确", "用户名或密码", "用户不存在", "帐号不存在", "账号不存在",
    "账号已停机", "账号欠费", "余额不足", "账号被锁定", "已锁定", "不允许", "禁用",
)
#: 「在线数超限」这类不算硬失败 —— 多提交一次就能把别的设备顶下去
_SOFT_HINTS = ("在线数", "已达上限", "超过最大", "重复认证", "踢下线", "在线设备")

#: 服务端自己吐出来的"超时"。**不是**账号密码问题，也**不值得**立刻再试一次 ——
#: 详见下面 login 里对它的处理。
_TIMEOUT_HINTS = ("超时", "timeout", "timed out", "连接超时")

_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
_ATTR_RE = {
    "name": re.compile(r"""\bname\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I),
    "type": re.compile(r"""\btype\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I),
    "value": re.compile(r"""\bvalue\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I),
}
_SKIP_TYPES = ("button", "submit", "reset", "file", "image")
_SID_RE = re.compile(r";JSESSIONID[^=;?&\"'<>\s]*=[^;?&\"'<>\s]+", re.I)
_COOKIE_SID_RE = re.compile(r"(?:^|;\s*)JSESSIONID[^=;]*=([^;]+)", re.I)
_ERR_MSG_RE = re.compile(r"""id\s*=\s*["']errMessage["'][^>]*value\s*=\s*["']([^"']*)["']""", re.I)
_ERR_MSG_RE2 = re.compile(r"""value\s*=\s*["']([^"']*)["'][^>]*id\s*=\s*["']errMessage["']""", re.I)
_URL_IN_TEXT_RE = re.compile(r"""https?://[^\s"'<>\\]+|/[A-Za-z0-9_\-./]*?(?:portal|webauth)\.do[^\s"'<>\\]*""", re.I)


def _plain(html: str) -> str:
    """把 HTML 压成一行纯文本，用来在响应里找失败原因。"""
    text = re.sub(r"(?s)<script.*?</script>", " ", html or "")
    text = re.sub(r"(?s)<style.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _attr(tag: str, key: str) -> str:
    match = _ATTR_RE[key].search(tag)
    if not match:
        return ""
    return next((g for g in match.groups() if g is not None), "")


def parse_form_fields(html: str) -> Dict[str, str]:
    """解析页面里所有 ``<input>`` 的 ``name → value``。

    跳过按钮 / 提交 / 文件这类不会作为数据提交的控件，**也跳过 ``disabled``
    的字段** —— 浏览器压根不会提交它们，多带过去反而可能把门户带偏
    （华为门户里 ``wlanuserip`` / ``urlParameter`` 这些恰好都是 disabled 的，
    它们的作用是写进表单 action，不是当 body 发）。
    """
    fields: Dict[str, str] = {}
    if not html:
        return fields
    for match in _INPUT_RE.finditer(html):
        tag = match.group(0)
        name = _attr(tag, "name")
        if not name.strip():
            continue
        if _attr(tag, "type").lower() in _SKIP_TYPES:
            continue
        if re.search(r"\bdisabled\b", tag, re.I):
            continue
        value = _attr(tag, "value")
        value = (value.replace("&amp;", "&").replace("&quot;", '"')
                 .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
        fields[name] = value
    return fields


def _pick(fields: Dict[str, str], candidates: Tuple[str, ...]) -> str:
    """在已解析的表单里按优先级找一个字段的**真实名字**（保留原始大小写）。"""
    lowered = {name.lower(): name for name in fields}
    for candidate in candidates:
        real = lowered.get(candidate)
        if real:
            return real
    return ""


class HuaweiPortalProvider(Provider):
    name = "huawei_portal"
    display_name = "华为 Portal（portal.do / webauth.do）"
    min_confidence = 0.5

    # ------------------------------------------------------------ 指纹
    @classmethod
    def detect(cls, ctx: DetectContext) -> float:
        score = 0.0
        low = ctx.lower_text
        url = (ctx.url or "").lower()

        if "webauth.do" in url or "webauth.do" in low:
            score += 0.85
        if "portal.do" in url or "portal.do" in low:
            score += 0.55
        if "urlparameter" in low:
            score += 0.35
        if 'name="userid"' in low or "name='userid'" in low or "userid=" in low:
            score += 0.3
        if 'name="passwd"' in low or "name='passwd'" in low:
            score += 0.3
        if "wlanacname" in low or "wlanacname" in url:
            score += 0.2
        if "wlanuserip" in low or "wlanuserip" in url:
            score += 0.15
        return min(score, 1.0)

    # ------------------------------------------------------------ 登录
    def login(self, portal: str, username: str, password: str,
              client_ip: str = "", mac: str = "") -> LoginResult:
        page_url, html, set_cookie = self._fetch_portal_page(portal, client_ip, mac)
        if not page_url:
            return LoginResult(False, self.name, "没能找到华为门户登录页（网关未跳转，也不在校园网内？）")

        fields = parse_form_fields(html)
        if not fields:
            return LoginResult(False, self.name, "认证页里没有解析到登录表单（字段数 0）",
                               endpoint=page_url, raw=_plain(html)[:300])

        self._fill_credentials(fields, username, password)

        url_param = fields.get("urlParameter", "")
        sid = self._session_id(html, set_cookie)
        target = self._submit_url(page_url, sid, url_param)

        body = urllib.parse.urlencode(list(fields.items()))
        self.log_fields(fields, target, sid)

        last_raw = ""
        last_msg = ""
        for attempt in (1, 2):
            try:
                resp = self.session.post(target, data=body, headers={
                    "Referer": page_url,
                    "Content-Type": "application/x-www-form-urlencoded",
                })
            except Exception as exc:  # noqa: BLE001
                return LoginResult(False, self.name, "提交认证请求失败：{}".format(exc), endpoint=target)

            last_raw = _plain(resp.text or "")[:400]
            # 先给服务端一点时间把认证状态落下去，再去验 —— 顺序不能反，
            # 提交完立刻探测多半还看不到效果。
            time.sleep(float(self.opt("submit_delay", 2)))

            if self._online():
                message = ("认证成功，网络已连通" if attempt == 1
                           else "认证成功（已把原先在线的设备顶下线）")
                # 上面那次 _online() 就是权威校验，runner 不用再验一遍
                return LoginResult(True, self.name, message, endpoint=target,
                                   raw=last_raw, verified=True)

            reason = self._failure_reason(resp.text or "")
            # 门户说「此IP已在线请勿重复认证」时，它其实是在告诉你「你已经通了」。
            # 这句话反过来还证明了一件事：**请求被完整解析了**，账号密码都到位了。
            if reason and self._is_already_online(reason):
                return LoginResult(True, self.name, "该 IP 已在线，无需重复认证",
                                   endpoint=target, raw=last_raw, already_online=True,
                                   verified=True)
            last_msg = reason or "提交后仍未联网（HTTP {}）".format(resp.status)

            if reason and self._is_hard(reason):
                return LoginResult(False, self.name, "认证失败：{}".format(reason),
                                   endpoint=target, raw=last_raw)
            if reason and self._is_timeout(reason):
                # 「认证超时」是**服务端**说的：它自己没在时限内拿到结果
                # （多半是在往上级 AAA 转发，或者本机刚关联上、会话还没建好）。
                # 同一秒再补一刀只会拿到同样一句超时，白花 submit_delay +
                # retry_delay + 又一次完整联网探测。直接交回去，
                # 让热身阶段下一轮重来 —— 那时服务端通常已经好了。
                return LoginResult(False, self.name, "认证失败：{}".format(reason),
                                   endpoint=target, raw=last_raw)
            if attempt == 1:
                time.sleep(float(self.opt("retry_delay", 1)))

        return LoginResult(False, self.name, "认证失败：{}".format(last_msg),
                           endpoint=target, raw=last_raw)

    # ------------------------------------------------------------ 找登录页
    def _candidate_pages(self, portal: str, client_ip: str = "", mac: str = ""):
        """按可靠度依次产出候选登录页地址。

        惰性设计：**前面的能拿到登录页，后面那些探测就一次都不会发** ——
        配置好的地址命中时，开机时不会再多花几秒去戳外网。
        """
        configured = str(self.opt("portal_page", "") or "").strip()
        if configured:
            yield self._fill_placeholders(configured, client_ip, mac)

        for trigger in TRIGGERS:
            try:
                resp = self.session.get(trigger, timeout=5, allow_redirects=False)
            except Exception:  # noqa: BLE001
                continue
            location = resp.location or ""
            if location and self._looks_portal(location):
                yield urllib.parse.urljoin(trigger, location)
                return
            found = self._find_url_in_text(resp.text or "")
            if found:
                yield urllib.parse.urljoin(trigger, found)
                return

        base = portal.rstrip("/") if portal else ""
        if base:
            for extra in ("/portal.do", "/webauth.do", "/"):
                yield base + extra

    def _fetch_portal_page(self, portal: str, client_ip: str = "", mac: str = ""):
        """返回 ``(登录页地址, 页面 HTML, Set-Cookie)``；找不到返回三个空串。

        华为门户认「页面里那张隐藏表单」，所以这里的判据是：**GET 回来
        确实是一张带 ``<input>`` 的页面**。302 的情况跟着 Location 走一次，
        但同样要落回一张表单才算数。
        """
        for candidate in self._candidate_pages(portal, client_ip, mac):
            try:
                resp = self.session.get(candidate, timeout=self.session.timeout,
                                        allow_redirects=False)
            except Exception:  # noqa: BLE001
                continue
            resolved, html = candidate, resp.text or ""
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.location or ""
                if location and self._looks_portal(location):
                    follow = urllib.parse.urljoin(candidate, location)
                    try:
                        resp = self.session.get(follow, timeout=self.session.timeout)
                        resolved, html = follow, resp.text or ""
                    except Exception:  # noqa: BLE001
                        continue
            if "<input" in html.lower() and parse_form_fields(html):
                return resolved, html, str(resp.headers.get("set-cookie", ""))
        return "", "", ""

    @staticmethod
    def _fill_placeholders(template: str, client_ip: str, mac: str) -> str:
        """支持 ``{ip}`` / ``{mac}`` 占位 —— 写配置时不必每次都改 IP。"""
        try:
            return template.format(ip=client_ip or "", mac=mac or "")
        except (KeyError, IndexError, ValueError):
            return template

    @staticmethod
    def _looks_portal(url: str) -> bool:
        low = url.lower()
        return "portal.do" in low or "webauth.do" in low

    @staticmethod
    def _find_url_in_text(text: str) -> str:
        for match in _URL_IN_TEXT_RE.finditer(text or ""):
            value = match.group(0).replace("\\/", "/").replace("&amp;", "&")
            if "portal.do" in value.lower() or "webauth.do" in value.lower():
                return value
        return ""

    # ------------------------------------------------------------ 组装提交
    @staticmethod
    def _fill_credentials(fields: Dict[str, str], username: str, password: str) -> None:
        """把账号密码**覆盖**进原字段（没有就新建），并勾上「记住我」。"""
        user_field = _pick(fields, USERNAME_FIELDS) or "userId"
        pass_field = _pick(fields, PASSWORD_FIELDS) or "passwd"
        fields[user_field] = username
        fields[pass_field] = password

        lowered = {name.lower(): name for name in fields}
        for key, value in (("isremind", "1"), ("reminfo", "on")):
            real = lowered.get(key)
            if real:
                fields[real] = value

    @staticmethod
    def _session_id(html: str, set_cookie: str = "") -> str:
        """取 JSESSIONID：优先页面里 URL 重写的那种（带分号前缀）。"""
        match = _SID_RE.search(html or "")
        if match:
            return match.group(0)
        match = _COOKIE_SID_RE.search(set_cookie or "")
        if match:
            return ";JSESSIONID=" + match.group(1)
        return ""

    @staticmethod
    def _submit_url(page_url: str, sid: str, url_param: str) -> str:
        parts = urllib.parse.urlsplit(page_url if "//" in page_url else "http://" + page_url)
        path = parts.path or "/"
        if "webauth.do" not in path.lower():
            path = "/webauth.do"
        target = "{scheme}://{netloc}{path}{sid}".format(
            scheme=parts.scheme or "http", netloc=parts.netloc, path=path, sid=sid)
        query = url_param or parts.query
        return target + ("?" + query if query else "")

    # ------------------------------------------------------------ 结果判定
    def _online(self) -> bool:
        """提交后自己验一次联网 —— runner 后面还会再验，这里为了尽早收工。"""
        from ..detector import check_online  # 延迟导入，避免 provider/detector 循环依赖

        try:
            return bool(check_online(self.session).online)
        except Exception:  # noqa: BLE001
            return False

    def _failure_reason(self, html: str) -> str:
        for pattern in (_ERR_MSG_RE, _ERR_MSG_RE2):
            match = pattern.search(html or "")
            if match and match.group(1).strip():
                return match.group(1).strip()
        text = _plain(html)
        for hint in _ALREADY_HINTS:
            if hint in text:
                return ""
        return ""

    @staticmethod
    def _is_hard(reason: str) -> bool:
        if any(hint in reason for hint in _SOFT_HINTS):
            return False
        return any(hint in reason for hint in _HARD_FAILURES)

    @staticmethod
    def _is_already_online(reason: str) -> bool:
        return any(hint in (reason or "") for hint in _ALREADY_HINTS)

    @staticmethod
    def _is_timeout(reason: str) -> bool:
        """服务端回的失败原因是不是"超时"这一类。"""
        low = (reason or "").lower()
        return any(hint in low for hint in _TIMEOUT_HINTS)

    def log_fields(self, fields: Dict[str, str], target: str, sid: str) -> None:
        """把解析到的字段名和提交地址打到 debug 日志里 —— 排障全靠它。"""
        names: List[str] = sorted(fields)
        logger = getattr(self.session, "logger", None)
        if callable(logger):
            logger("  表单字段（{} 个）：{}".format(len(names), ", ".join(names)))
            logger("  提交地址：{}{}".format(target, "（带会话号）" if sid else "（无会话号）"))
