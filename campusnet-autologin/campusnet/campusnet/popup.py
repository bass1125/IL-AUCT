"""关掉 Windows 自动弹出的强制门户（captive portal）登录页。

**这不是本程序造成的，是 Windows 的"按设计行为"。**

Windows 用 NCSI 判断联网状态：向 ``http://www.msftconnecttest.com/connecttest.txt``
发一个 HTTP GET。校园网在"已经连上、但还没通过认证"的那几秒里会把请求劫持、
重定向到门户页，于是 Windows 判定"这个网络需要登录"（事件日志里写的是
``已在接口 ... 上检测到热点``），并**自动打开默认浏览器**到
``http://www.msftconnecttest.com/redirect`` —— 该地址又被门户劫持成登录页。
微软 KB 4494446 写得很直白：这是 by design，理由是"改善用户体验"。

问题在于：本程序已经把网自动登录好了，这张页面就成了纯多余的东西 ——
每次开机都弹一下、还一直杵在桌面上。

所以守护在**确认网络已经通了**之后，顺手把这个窗口关掉。判据放得很严，
不会误伤用户自己开的浏览器：

* 窗口标题必须含有系统那张登录页的特征词（"网络认证" / "Sign in to network" …）；
* 窗口必须属于已知的浏览器进程；
* 只在"网络确实通了"之后动手；
* 认证没成功时**绝不**动手 —— 那时这张页面是用户唯一的出路。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, List, Optional, Tuple

LogFn = Optional[Callable[[str, str], None]]

#: 系统那张登录页的窗口标题里会出现的特征词（统一小写后比对）。
_TITLE_MARKERS = (
    "网络认证",
    "登录到网络",
    "网络登录",
    "无线网络认证",
    "sign in to network",
    "log in to network",
    "network sign-in",
    "sign in to wi-fi",
    "sign in to wifi",
    "hotspot sign in",
    "captive portal",
)

#: 只关这些浏览器的窗口；进程名不在名单里的一律不碰。
_BROWSER_EXES = frozenset((
    "msedge.exe",
    "chrome.exe",
    "firefox.exe",
    "iexplore.exe",
    "brave.exe",
    "opera.exe",
    "vivaldi.exe",
    "360se.exe",
    "360chrome.exe",
    "qqbrowser.exe",
))

WM_CLOSE = 0x0010
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# --------------------------------------------------------------------- 开关
def _truthy(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y", "是", "开"):
        return True
    if text in ("0", "false", "no", "off", "n", "否", "关"):
        return False
    return default


def enabled(options) -> bool:
    """配置里是否开启了「自动关掉系统弹出的登录页」。默认开。"""
    try:
        return _truthy((options or {}).get("close_portal_popup"), True)
    except AttributeError:
        return True


# --------------------------------------------------------------------- Windows
if os.name == "nt":                                # pragma: no cover - 平台相关
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _kernel32.QueryFullProcessImageNameW.argtypes = (
        ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    _user32.EnumWindows.argtypes = (ctypes.c_void_p, wintypes.LPARAM)
    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _process_name(pid: int) -> str:
        """取进程的可执行文件名（拿不到就返回空串）。"""
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value)
            return ""
        finally:
            _kernel32.CloseHandle(ctypes.c_void_p(handle))


#: 浏览器给窗口标题加的后缀分隔符（"网络认证 - 个人 - Microsoft Edge"）。
_SUFFIX_SEPS = (" - ", " – ", " — ", "-", "–", "—")


def looks_like_portal_page(title: str) -> bool:
    """标题看着像系统那张强制门户登录页吗。

    匹配要求**整个标题就是特征词**，或者"特征词 + 浏览器后缀" ——
    不用"包含"是刻意的：``网络认证记录表`` 这种用户自己的页面名也含那几个字，
    包含匹配会把它一起关掉。
    """
    text = " ".join((title or "").split()).strip().lower()
    if not text:
        return False
    for marker in _TITLE_MARKERS:
        if text == marker:
            return True
        if any(text.startswith(marker + sep) for sep in _SUFFIX_SEPS):
            return True
    return False


def _collect_targets() -> List[Tuple[int, str]]:
    """找到所有"像是系统弹出的登录页"的顶层窗口。"""
    found: List[Tuple[int, str]] = []

    def _on_window(hwnd, _lparam):
        try:
            if not _user32.IsWindowVisible(hwnd):
                return True
            length = _user32.GetWindowTextLengthW(hwnd)
            if not length or length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            _user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            if not looks_like_portal_page(title):
                return True
            pid = wintypes.DWORD(0)
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if _process_name(pid.value).lower() not in _BROWSER_EXES:
                return True
            found.append((hwnd, title))
        except Exception:                          # noqa: BLE001 - 回调里绝不能抛
            pass
        return True

    _user32.EnumWindows(_WNDENUMPROC(_on_window), 0)
    return found


def close_now() -> List[str]:
    """扫一遍并关掉能认出来的登录页窗口，返回被关掉的标题。"""
    if os.name != "nt":
        return []
    closed: List[str] = []
    for hwnd, title in _collect_targets():
        try:
            if _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
                closed.append(" ".join(title.split()))
        except Exception:                          # noqa: BLE001
            pass
    return closed


# --------------------------------------------------------------------- 后台扫描
_state_lock = threading.Lock()
_sweeping = False

#: 扫多久、多久扫一次。浏览器是"先开窗口、后渲染页面"，标题得等门户页加载
#: 出来才有，所以只扫一次不够 —— 得盯一段时间。
SWEEP_SECONDS = 45.0
SWEEP_GAP = 2.0


def sweep_async(duration: float = SWEEP_SECONDS, gap: float = SWEEP_GAP,
                log: LogFn = None) -> bool:
    """后台盯一段时间，把冒出来的登录页窗口都关掉。

    返回 False 表示已经有一个扫描在跑（不重复起线程）。
    """
    global _sweeping
    with _state_lock:
        if _sweeping:
            return False
        _sweeping = True

    def _worker() -> None:
        global _sweeping
        deadline = time.time() + max(1.0, float(duration))
        total = 0
        try:
            while time.time() < deadline:
                closed = close_now()
                if closed:
                    total += len(closed)
                    if log:
                        log("已关掉系统自动弹出的登录页（{}）".format("、".join(closed)), "ok")
                time.sleep(max(0.5, float(gap)))
        except Exception as exc:                   # noqa: BLE001 - 后台线程不能带崩守护
            if log:
                log("关闭登录页窗口时出错：{}".format(exc), "debug")
        finally:
            with _state_lock:
                _sweeping = False

    threading.Thread(target=_worker, name="campusnet-popup-sweep", daemon=True).start()
    return True
