# -*- coding: utf-8 -*-
"""真实探测：用 campusnet 的 provider 去访问真的校园网门户，但不提交。

先看清"解析出来的字段到底对不对"，再决定要不要真的提交。
"""

from __future__ import annotations

import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

REPO = r"D:\i_Lian\campusnet-autologin\campusnet"
sys.path.insert(0, REPO)

from campusnet.providers.huawei_portal import (  # noqa: E402
    HuaweiPortalProvider, parse_form_fields, _pick, USERNAME_FIELDS, PASSWORD_FIELDS,
)
from campusnet.session import Session  # noqa: E402

PAGE = ("http://1.1.1.1:8888/webauth.do?wlanacip=112.53.87.243&wlanacname=TPsdcmc"
        "&wlanuserip={ip}&mac=04:ed:33:58:fd:a4&vlan=3605"
        "&url=http://www.msftconnecttest.com/redirect")


def main() -> int:
    session = Session(timeout=15)
    provider = HuaweiPortalProvider(session, {"portal_page": PAGE})

    url, html, set_cookie = provider._fetch_portal_page(
        "http://1.1.1.1:8888/", "10.173.176.248", "04ed3358fda4")

    print("登录页地址 :", url)
    print("Set-Cookie :", set_cookie)
    print("页面长度   :", len(html))
    print("会话号     :", repr(provider._session_id(html, set_cookie)))
    print("Cookie 罐  :", [c.name for c in session.cookies])
    print()

    fields = parse_form_fields(html)
    print("解析到 {} 个可提交字段：".format(len(fields)))
    for key, value in fields.items():
        print("   {:<22} = {}".format(key, value[:70]))

    print()
    user_field = _pick(fields, USERNAME_FIELDS)
    pass_field = _pick(fields, PASSWORD_FIELDS)
    print("识别到的账号字段 :", repr(user_field))
    print("识别到的密码字段 :", repr(pass_field))
    print("表单 action      :", "(由 _submit_url 拼)")

    probe = dict(fields)
    provider._fill_credentials(probe, "26370217900251", "253117")
    print("覆盖后 userId    :", repr(probe.get(user_field)))
    print("覆盖后 passwd    :", repr(probe.get(pass_field)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
