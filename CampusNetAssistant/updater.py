# -*- coding: utf-8 -*-
"""IL AUCT 自动更新。

三件事，分开做：

* **查** —— 调 ``api.github.com`` 看有没有新 Release；
* **下** —— ``github.com`` 主域名在国内经常连不上，下载链接统一套一层国内加速；
* **换** —— Windows 不允许运行中的 exe 覆盖自己，只能生成一个批处理，
  等本进程退出后再接手：覆盖 → 重启。

只用标准库，不给这个项目添依赖。
"""

from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 配置
#: 仓库坐标。换仓库时只改这两行。
OWNER = "MiaoBoss"
REPO = "IL-AUCT"

_API = "https://api.github.com"
_RELEASES = "{}/repos/{}/{}/releases/latest".format(_API, OWNER, REPO)

#: 国内加速镜像。
#:
#: Release 的下载地址挂在 github.com 上，而 github.com 在国内常常直连不通
#: （本项目开发机上就是 502）。套上这个前缀后由镜像转发，实测可用。
#: 想换别的镜像，只要保证它支持 ``镜像前缀 + 原始 github 地址`` 这种拼法。
PROXY = "https://gh-proxy.com/"

_UA = "IL-AUCT-Updater (+https://github.com/{}/{})".format(OWNER, REPO)

CHECK_TIMEOUT = 15
#: 安装包约 9MB，校园网慢起来没边，超时给宽裕些。
DOWNLOAD_TIMEOUT = 600


# ---------------------------------------------------------------- 网络
def _ssl_context() -> ssl.SSLContext:
    """PyInstaller 打包后默认的证书查找路径会失效，显式加载系统证书库。

    少了这一步，打包出来的 exe 一联网就 ``CERTIFICATE_VERIFY_FAILED``。
    """
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
    except Exception:            # noqa: BLE001 - 拿不到就用默认的
        pass
    return ctx


def _open(url: str, timeout: float):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/vnd.github+json, application/octet-stream",
    })
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


# ---------------------------------------------------------------- 版本
def parse_version(text: str) -> tuple:
    """``v1.2.3`` / ``1.2.3-beta`` → ``(1, 2, 3)``。"""
    return tuple(int(n) for n in re.findall(r"\d+", str(text or ""))[:4]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    """latest 是否比 current 新（按数字段逐位比较）。"""
    a, b = parse_version(latest), parse_version(current)
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    return a > b


def _pick_asset(assets) -> dict:
    """从 Release 附件里挑主程序 exe（排除卸载器那种）。"""
    exes = [a for a in assets if str(a.get("name") or "").lower().endswith(".exe")]
    if not exes:
        return {}

    def rank(asset):
        name = str(asset.get("name") or "")
        lower = name.lower()
        is_uninstall = ("uninstall" in lower) or ("卸载" in name)
        return (
            0 if is_uninstall else 1,            # 先排除卸载器
            1 if "il auct" in lower else 0,      # 再优先主程序
            int(asset.get("size") or 0),         # 最后挑大的
        )

    return max(exes, key=rank)


# ---------------------------------------------------------------- 检查
def check(current: str) -> dict:
    """查最新 Release。

    **永远返回 dict，不抛异常** —— 更新只是锦上添花，网络不通不该让界面炸掉。
    """
    out = {
        "ok": False, "available": False, "current": current, "latest": "",
        "notes": "", "size": 0, "url": "", "asset_name": "", "page": "",
        "published": "", "error": "",
    }

    try:
        with _open(_RELEASES, CHECK_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            out["ok"] = True                     # 还没发过版本，不算错误
            return out
        out["error"] = "更新服务器返回 HTTP {}".format(exc.code)
        return out
    except Exception as exc:                     # noqa: BLE001
        out["error"] = "连不上更新服务器（{}）".format(type(exc).__name__)
        return out

    latest = str(data.get("tag_name") or "").lstrip("vV")
    asset = _pick_asset(data.get("assets") or [])
    out.update({
        "ok": True,
        "available": bool(latest) and is_newer(latest, current),
        "latest": latest,
        "notes": str(data.get("body") or "").strip(),
        "size": int(asset.get("size") or 0),
        "url": str(asset.get("browser_download_url") or ""),
        "asset_name": str(asset.get("name") or ""),
        "page": str(data.get("html_url") or ""),
        "published": str(data.get("published_at") or ""),
    })
    return out


# ---------------------------------------------------------------- 下载
def download(url: str, dest: str, on_progress=None) -> tuple:
    """下载安装包。返回 ``(成功?, 错误信息)``。

    ``on_progress(done, total)`` 会被反复回调；服务端没给 Content-Length 时
    ``total`` 为 0，调用方需自行容错。
    """
    if not url:
        return False, "这个版本没有可下载的安装包"

    real = url if url.startswith(PROXY) else PROXY + url
    part = dest + ".part"
    try:
        with _open(real, DOWNLOAD_TIMEOUT) as resp:
            try:
                total = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
            done = 0
            with open(part, "wb") as handle:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if on_progress:
                        on_progress(done, total)
        if not os.path.getsize(part):
            raise OSError("下载到的文件是空的")
        os.replace(part, dest)
        return True, ""
    except Exception as exc:                     # noqa: BLE001
        try:
            os.remove(part)
        except OSError:
            pass
        return False, "下载失败：{}".format(
            exc if isinstance(exc, OSError) else type(exc).__name__)


# ---------------------------------------------------------------- 替换
#: 替换脚本。
#:
#: 关键在 ``copy`` 那一步：目标 exe 还被占用时 copy 会失败，等一秒再试，
#: 成功就说明占用它的进程（本程序 + 守护）都退干净了。
#:
#: 两个刻意的写法：
#:   · 延时用 System32 下的**绝对路径**调 timeout —— PATH 里要是躺着 Git 自带的
#:     Unix ``timeout``，光写 ``timeout /t 1`` 会被它抢走并报 "invalid time interval"，
#:     于是延时失效、变成忙等，几百次重试瞬间耗光（本机就踩到了）。
#:   · 不用 ``tasklist`` 判进程 —— 有的机器上它被安全软件拦。
_APPLY_BAT = """@echo off
setlocal
title IL AUCT - applying update

set "SRC={src}"
set "DST={dst}"
set "SYS=%SystemRoot%\\System32"
set /a TRIES=0

:wait
set /a TRIES+=1
if %TRIES% gtr 300 goto :giveup

copy /y "%SRC%" "%DST%" >nul 2>&1
if not errorlevel 1 goto :done

if exist "%SYS%\\timeout.exe" (
    "%SYS%\\timeout.exe" /t 1 /nobreak >nul 2>&1
) else (
    "%SYS%\\ping.exe" -n 2 127.0.0.1 >nul 2>&1
)
goto :wait

:done
start "" "%DST%"
del /f /q "%SRC%" >nul 2>&1
(goto) 2>nul & del "%~f0"
exit /b 0

:giveup
exit /b 1
"""


def update_dir() -> str:
    """放新版本和替换脚本的地方。"""
    path = os.path.join(tempfile.gettempdir(), "IL_AUCT_update")
    os.makedirs(path, exist_ok=True)
    return path


def staged_path() -> str:
    """下载好的新 exe 落到哪。"""
    return os.path.join(update_dir(), "IL AUCT.new.exe")


def write_script(target_exe: str, new_exe: str) -> str:
    """生成替换脚本（GBK + CRLF —— cmd 就吃这一套），返回脚本路径。"""
    # cmd 对正斜杠只在部分命令里宽容，统一成反斜杠省心
    body = _APPLY_BAT.format(src=os.path.normpath(new_exe),
                             dst=os.path.normpath(target_exe))
    body = body.replace("\r\n", "\n").replace("\n", "\r\n")
    script = os.path.join(update_dir(), "apply_update.bat")
    with open(script, "wb") as handle:
        handle.write(body.encode("gbk", "replace"))
    return script


def launch(script: str) -> bool:
    """脱离本进程把替换脚本跑起来。"""
    #: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    flags = 0x00000008 | 0x00000200
    env = os.environ.copy()
    # 打包版会把这些变量留给子进程，子进程拿去复用解压目录会出乱子
    for key in ("_MEIPASS", "_MEIPASS2", "_PYI_APPLICATION_HOME_DIR",
                "_PYI_PARENT_PROCESS_LEVEL", "_PYI_ARCHIVE_FILE",
                "_PYI_SPLASH_IPC"):
        env.pop(key, None)
    try:
        subprocess.Popen(
            ["cmd", "/c", script],
            creationflags=flags, close_fds=True, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, cwd=tempfile.gettempdir(),
        )
        return True
    except Exception:            # noqa: BLE001
        return False


def cleanup() -> None:
    """清掉上次更新留下的残渣（新版本已就位、脚本已跑完的情况）。"""
    for name in ("IL AUCT.new.exe", "apply_update.bat"):
        path = os.path.join(update_dir(), name)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
