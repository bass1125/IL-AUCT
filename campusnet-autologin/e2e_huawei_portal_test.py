# -*- coding: utf-8 -*-
"""端到端验证：起一个"假的华为 Portal"，让 campusnet 真的去认证它。

重点不是"能不能连上真服务器"（那个做不到），而是**我们发出去的请求长什么样**：
字段名、值、有没有重复、会话号有没有带上。这类工具翻车 99% 就翻在这儿。
"""

from __future__ import annotations

import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "campusnet"))

import campusnet.detector as detector  # noqa: E402
from campusnet.providers import fingerprint, get_provider  # noqa: E402
from campusnet.providers.base import DetectContext  # noqa: E402
from campusnet.providers.huawei_portal import HuaweiPortalProvider, parse_form_fields  # noqa: E402
from campusnet.session import Session  # noqa: E402

PORT = 18899
RECEIVED = {}
USER = "26370217900251"
PWD = "253117"
SID = ";JSESSIONID-BOSS-0=A1B2C3D4E5F6"

#: 模拟真实华为门户的登录页：一堆 hidden 字段 + 空的 userId/passwd
LOGIN_HTML = """<!DOCTYPE html><html><head><title>校园网认证</title></head><body>
<form name="form1" method="post" action="webauth.do" onSubmit="return check()">
<input type="hidden" name="wlanacname" value="TPsdcmc">
<input type="hidden" name="wlanacip" value="112.53.87.243">
<input id="wlanuserip" name="wlanuserip" type="hidden" value="10.173.176.248" disabled="disabled">
<input type="hidden" name="mac" value="04ed3358fda4">
<input type="hidden" name="vlan" value="3605">
<input type="hidden" name="nasip" value="112.53.87.243">
<input type="hidden" name="ssid" value="">
<input id="urlParameter" name="urlParameter" type="hidden" value="wlanacname=TPsdcmc&wlanacip=112.53.87.243&wlanuserip=10.173.176.248&mac=04ed3358fda4&vlan=3605" disabled="disabled">
<input type="hidden" name="version" value="2.0">
<input id="tservertypeid" type="hidden" value="axe" disabled="disabled">
<input type="text" name="userId" value="">
<input type="password" name="passwd" value="">
<input type="checkbox" name="isRemind" value="0">
<input type="checkbox" name="remInfo" value="off">
<input type="button" name="submitBtn" value="登录">
<input type="submit" name="buttonClicked" value="4">
</form>
<script>var u = "/webauth.do;JSESSIONID-BOSS-0=A1B2C3D4E5F6?wlanuserip=10.173.176.248";</script>
</body></html>"""

#: 换个字段命名（华为某些版本）—— 用来验证"自适应字段名"
LOGIN_HTML_ALT = LOGIN_HTML.replace('name="userId"', 'name="userName"').replace(
    'name="passwd"', 'name="passWord"')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body: bytes, cookie: str = ""):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/jump", "/trigger"):
            # 模拟「网关把请求 302 到门户」—— 真实场景里 Location 上就带着 wlanuserip 这些参数
            self.send_response(302)
            self.send_header("Location", "/portal.do?via={}&wlanacname=TPsdcmc&wlanuserip=10.173.176.248".format(
                parsed.path.strip("/")))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if parsed.path.endswith("portal.do"):
            page = LOGIN_HTML_ALT if "alt" in parsed.query else LOGIN_HTML
            self._send(page.encode("utf-8"), cookie="JSESSIONID=COOKIEONLY; Path=/")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        RECEIVED["path"] = self.path
        RECEIVED["body"] = raw
        RECEIVED["cookie"] = self.headers.get("Cookie") or ""
        RECEIVED["referer"] = self.headers.get("Referer") or ""
        RECEIVED["count"] = RECEIVED.get("count", 0) + 1

        REPEATED = "此IP已在线请勿重复认证"

        if "online=1" in (self.headers.get("Referer") or ""):
            body = '<html><input type="hidden" id="errMessage" value="{}"></html>'.format(REPEATED)
            self._send(body.encode("utf-8"))
            return

        qs = parse_qs(raw)
        ok = qs.get("userId") == [USER] and qs.get("passwd") == [PWD]
        body = ("<html><body>认证成功</body></html>" if ok else
                '<html><input type="hidden" id="errMessage" value="用户名或密码错误"></html>')
        self._send(body.encode("utf-8"))


class Check:
    def __init__(self):
        self.failed = 0

    def that(self, label, condition, detail=""):
        mark = "OK  " if condition else "FAIL"
        if not condition:
            self.failed += 1
        print("[{}] {}{}".format(mark, label, ("  →  " + str(detail)) if detail else ""))


def start_server():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


DEFAULT_QUERY = "wlanacname=TPsdcmc&wlanacip=112.53.87.243&wlanuserip=10.173.176.248&mac=04ed3358fda4&vlan=3605"


def make_provider(page_query=DEFAULT_QUERY):
    session = Session(timeout=5)
    options = {"portal_page": "http://127.0.0.1:{}/portal.do?{}".format(PORT, page_query)}
    return HuaweiPortalProvider(session, options), session


def main():
    server = start_server()
    check = Check()
    base_portal = "http://127.0.0.1:{}/".format(PORT)

    print("=" * 68)
    print("1) 表单元解析：hidden / 按钮该跳过的跳过")
    print("=" * 68)
    fields = parse_form_fields(LOGIN_HTML)
    check.that("解析到 hidden 字段 wlanacname", fields.get("wlanacname") == "TPsdcmc")
    check.that("解析到空账号框 userId", "userId" in fields and fields["userId"] == "")
    check.that("解析到空密码框 passwd", "passwd" in fields and fields["passwd"] == "")
    check.that("按 type=button 跳过 submitBtn", "submitBtn" not in fields)
    check.that("按 type=submit 跳过 buttonClicked", "buttonClicked" not in fields)
    check.that("保留 checkbox isRemind", "isRemind" in fields)
    check.that("跳过 disabled 的 wlanuserip（浏览器也不提交）", "wlanuserip" not in fields)
    check.that("跳过 disabled 的 urlParameter", "urlParameter" not in fields)

    print()
    print("=" * 68)
    print("2) 指纹识别：能不能认出这是华为 Portal")
    print("=" * 68)
    ctx = DetectContext(url=base_portal + "webauth.do?wlanacname=TPsdcmc&wlanuserip=10.173.176.248",
                        text=LOGIN_HTML)
    scores = dict(fingerprint(ctx))
    check.that("huawei_portal 参与打分", "huawei_portal" in scores, scores)
    check.that("huawei_portal 是最高分", scores.get("huawei_portal", 0) == max(scores.values()) if scores else False,
               scores)
    provider_order = [name for name, _ in fingerprint(ctx)]
    check.that("排第一位的就是它", provider_order and provider_order[0] == "huawei_portal", provider_order)

    print()
    print("=" * 68)
    print("3) 登录全流程：提交内容必须完全正确")
    print("=" * 68)
    detector.check_online = lambda session, portal_hint="": detector.NetStatus(online=True)
    provider, _ = make_provider()
    result = provider.login(base_portal, USER, PWD, "10.173.176.248", "04ed3358fda4")

    check.that("登录返回成功", result.ok, result.message)
    qs = parse_qs(RECEIVED.get("body", ""))
    check.that("POST 到了 webauth.do", "webauth.do" in RECEIVED.get("path", ""), RECEIVED.get("path"))
    check.that("URL 里带上了 JSESSIONID 会话号",
               SID.split("=")[0][1:] in RECEIVED.get("path", ""), RECEIVED.get("path"))
    check.that("URL 里带上了网关参数", "wlanuserip=10.173.176.248" in RECEIVED.get("path", ""),
               RECEIVED.get("path"))
    check.that("账号写进了 userId", qs.get("userId") == [USER], qs.get("userId"))
    check.that("密码写进了 passwd", qs.get("passwd") == [PWD], qs.get("passwd"))
    check.that("userId 只出现一次（覆盖而不是追加）",
               RECEIVED.get("body", "").count("userId=") == 1,
               RECEIVED.get("body", "").count("userId="))
    check.that("passwd 只出现一次", RECEIVED.get("body", "").count("passwd=") == 1)
    check.that("hidden 字段一个没丢：wlanacname", qs.get("wlanacname") == ["TPsdcmc"])
    check.that("hidden 字段一个没丢：vlan", qs.get("vlan") == ["3605"])
    check.that("body 里不含 disabled 字段（浏览器也不发）",
               "urlParameter" not in RECEIVED.get("body", "") and "wlanuserip" not in RECEIVED.get("body", ""),
               RECEIVED.get("body", ""))
    check.that("勾上了「记住我」isRemind=1", qs.get("isRemind") == ["1"], qs.get("isRemind"))
    check.that("remInfo=on", qs.get("remInfo") == ["on"], qs.get("remInfo"))
    check.that("带上了 Set-Cookie 里的会话", "JSESSIONID" in RECEIVED.get("cookie", ""),
               RECEIVED.get("cookie"))
    check.that("带上了 Referer", RECEIVED.get("referer", "").endswith("portal.do?") or
               "portal.do" in RECEIVED.get("referer", ""), RECEIVED.get("referer"))
    check.that("只提交了一次（一次就成了，不瞎重试）", RECEIVED.get("count") == 1, RECEIVED.get("count"))

    print()
    print("=" * 68)
    print("4) 字段改名成了 userName / passWord 也要能自适应")
    print("=" * 68)
    RECEIVED.clear()
    provider2, _ = make_provider("alt=1")
    result2 = provider2.login(base_portal, USER, PWD, "10.173.176.248", "04ed3358fda4")
    qs2 = parse_qs(RECEIVED.get("body", ""))
    check.that("改名后仍登录成功", result2.ok, result2.message)
    check.that("写进了 userName", qs2.get("userName") == [USER], qs2.get("userName"))
    check.that("写进了 passWord", qs2.get("passWord") == [PWD], qs2.get("passWord"))
    check.that("没有凭空多出 userId 字段", "userId" not in qs2)

    print()
    print("=" * 68)
    print("5) 密码错误时：把门户给的失败原因读出来")
    print("=" * 68)
    RECEIVED.clear()
    detector.check_online = lambda session, portal_hint="": detector.NetStatus(online=False)
    provider3, _ = make_provider()
    result3 = provider3.login(base_portal, USER, "wrong-password", "10.173.176.248", "04ed3358fda4")
    check.that("返回失败", not result3.ok)
    check.that("读到「用户名或密码错误」", "用户名或密码错误" in result3.message, result3.message)

    print()
    print("=" * 68)
    print("5b) 门户回「此IP已在线请勿重复认证」——这其实是成功")
    print("=" * 68)
    RECEIVED.clear()
    detector.check_online = lambda session, portal_hint="": detector.NetStatus(online=False)
    provider5b, _ = make_provider("online=1")
    result5b = provider5b.login(base_portal, USER, PWD, "10.173.176.248", "04ed3358fda4")
    check.that("判定为成功", result5b.ok, result5b.message)
    check.that("标记成 already_online", result5b.already_online)
    check.that("只提交了一次（不会傻乎乎重试）", RECEIVED.get("count") == 1, RECEIVED.get("count"))

    print()
    print("=" * 68)
    print("6) 第一次没通、第二次才成 —— 模拟「把别的设备顶下线」")
    print("=" * 68)
    RECEIVED.clear()
    state = {"n": 0}

    def flaky(session, portal_hint=""):
        state["n"] += 1
        return detector.NetStatus(online=state["n"] >= 2)

    detector.check_online = flaky
    provider4, _ = make_provider()
    result4 = provider4.login(base_portal, USER, PWD, "10.173.176.248", "04ed3358fda4")
    check.that("第二次提交后成功", result4.ok, result4.message)
    check.that("确实提交了两次", RECEIVED.get("count") == 2, RECEIVED.get("count"))
    check.that("提示里说明了是顶下线", "顶下线" in result4.message, result4.message)

    print()
    print("=" * 68)
    print("7) 配置的地址是一条 302 跳转，要跟到真正的登录页")
    print("=" * 68)
    RECEIVED.clear()
    detector.check_online = lambda session, portal_hint="": detector.NetStatus(online=True)
    provider5 = HuaweiPortalProvider(Session(timeout=5),
                                     {"portal_page": "http://127.0.0.1:{}/jump".format(PORT)})
    result5 = provider5.login(base_portal, USER, PWD, "10.173.176.248", "04ed3358fda4")
    check.that("跟着 302 找到了登录页并登录成功", result5.ok, result5.message)
    qs5 = parse_qs(RECEIVED.get("body", ""))
    check.that("提交的是 webauth.do", "webauth.do" in RECEIVED.get("path", ""), RECEIVED.get("path"))
    check.that("账号密码正确带上", qs5.get("userId") == [USER] and qs5.get("passwd") == [PWD])

    print()
    print("=" * 68)
    print("8) 没配任何地址，靠网关 302 自己找（学校换地址了也能活）")
    print("=" * 68)
    RECEIVED.clear()
    import campusnet.providers.huawei_portal as hp  # noqa: E402

    original_triggers = hp.TRIGGERS
    hp.TRIGGERS = ("http://127.0.0.1:{}/trigger".format(PORT),)
    try:
        provider6 = HuaweiPortalProvider(Session(timeout=5), {})
        result6 = provider6.login(base_portal, USER, PWD, "10.173.176.248", "04ed3358fda4")
    finally:
        hp.TRIGGERS = original_triggers
    check.that("自动捕获网关跳转并登录成功", result6.ok, result6.message)
    qs6 = parse_qs(RECEIVED.get("body", ""))
    check.that("提交 URL 从 Location 里带上了 wlanuserip",
               "wlanuserip=10.173.176.248" in RECEIVED.get("path", ""), RECEIVED.get("path"))
    check.that("账号密码正确带上", qs6.get("userId") == [USER] and qs6.get("passwd") == [PWD])

    server.shutdown()
    print()
    print("=" * 68)
    print("失败项：{}".format(check.failed))
    print("=" * 68)
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
