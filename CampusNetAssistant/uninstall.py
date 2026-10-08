"""IL AUCT 卸载器 —— 把这套软件从机器上干净地移除。

和主程序共用同一套窗口外壳（本地 HTTP 服务 + 系统 Edge 的 app 模式），
所以看起来是同一个软件的延续，不是随便糊的一个黑框。

卸载按顺序做这些事：

    1. 停掉后台守护进程（就是那个开机自动登录的东西）
    2. 移除开机自启 —— 注册表 Run 项 + 任务管理器留下的「禁用」标记
    3. 清掉早期脚本版遗留在「启动」文件夹里的文件
    4. 清掉历史上没删干净的临时解压目录（可能好几百兆）
    5. 删除程序文件
    6. 可选：连账号密码（config.json 里是明文）和运行日志一起删
    7. 最后删自己

**第 7 条要绕一下**：Windows 不允许删掉正在运行的 exe，所以写一个临时
批处理，等本进程退出之后再动手。这个批处理刻意用纯 ASCII 短路径 +
GBK 编码 + CRLF 行尾 —— 之前吃过亏：UTF-8 无 BOM 的脚本被系统按 GBK
读，中文路径全成乱码，命令就指到不存在的文件上去了。

安全上的取舍：只删本程序自己认识的文件（见 OWN_FILES / OWN_DIRS），
目录里别的东西——比如你自己放进去的图片——原样留着，并在界面上列出来。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_FROZEN = bool(getattr(sys, "frozen", False))

if not _FROZEN:
    _REPO = os.path.join(os.path.dirname(_HERE), "campusnet-autologin", "campusnet")
    if os.path.isdir(_REPO) and _REPO not in sys.path:
        sys.path.insert(0, _REPO)

# 复用主程序的外壳：找 Edge、挑空闲端口、HTTP 处理器、临时目录清理。
# 这样窗口行为和主程序完全一致，不用维护两份。
import app as shell  # noqa: E402

APP_TITLE = "IL AUCT 卸载器"

#: 本程序自己的文件 —— 只有这些会被删
OWN_FILES = (
    "IL AUCT.exe",
    "校园网助手.exe",          # 改名前的老版本
    "IL AUCT.spec",
    "校园网助手.spec",
    "卸载 IL AUCT.spec",
    "uninstall.py",
    "app.ico",
    "使用说明.txt",
    "界面预览.png",
    "卸载界面预览.png",
    "开机加速.bat",            # 「开机加速」脚本（装登录计划任务用）
    "install_autologin.ps1",   # ↑ 实际干活的 PowerShell 脚本
    "取消开机加速.bat",
    "remove_autologin.ps1",
)

#: 打包后 app.py 不在 exe 里，只在源码目录里存在，所以单独列 ——
#: 之所以不跟上面放一起，是因为 "app.py" 这名字在别人的 Python 项目里太常见了，
#: 单独列出来提醒自己：删它之前一定要先确认这是本程序的安装目录。
OWN_SOURCES = ("app.py",)

#: web 目录不是整个删 —— 只删我们自己那几个文件，删空了才收掉目录。
#: 理由同上："web" 也是烂大街的目录名，整个 rmtree 太狠了。
WEB_FILES = ("index.html", "style.css", "app.js", "uninstall.html",
             "uninstall.js", "favicon.ico", "logo.png")

#: 会被整个删掉的目录（只剩名字足够独特、且删了无害的）
OWN_DIRS = ("__pycache__",)

#: 卸载器自己的名字 —— 永远不会被当成"用户的东西"列进保留清单，
#: 它由最后那个批处理负责删
SELF_NAMES = ("卸载 IL AUCT.exe",)

WATCH_LOCK = "campusnet-watch"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\StartupApproved\Run")
AUTOSTART_VALUE = "campusnet"      # 注册表里的值名，和主程序保持一致

#: 「开机加速」脚本安装的登录计划任务名，和 install_autologin.ps1 里保持一致。
#: 它不是必须的（不装也能开机自连），所以删不掉也不算失败。
TASK_NAME = "IL AUCT Autologin"

_DETACHED = 0x00000008
_NO_WINDOW = 0x08000000


# ------------------------------------------------------------------ 小工具
def _frozen_exe() -> str:
    """正在运行的这个 exe 的完整路径（开发模式下为空）。"""
    return os.path.abspath(sys.executable) if _FROZEN else ""


def _is_self(path: str) -> bool:
    me = _frozen_exe()
    return bool(me) and os.path.normcase(os.path.abspath(path)) == os.path.normcase(me)


def _program_dir() -> str:
    """程序目录 = 本 exe 所在的目录（打包后不能拿 _MEIPASS，那是临时解压目录）。"""
    if _FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return _HERE


def _local_app_dir() -> str:
    """Edge 用的那个私有浏览器配置目录（缓存几个兆，卸载时一并清掉）。"""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "CampusNetAssistant")


def _dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _mb(size: int) -> str:
    if size >= 1073741824:
        return "{:.2f} GB".format(size / 1073741824)
    if size >= 1048576:
        return "{:.1f} MB".format(size / 1048576)
    if size >= 1024:
        return "{:.0f} KB".format(size / 1024)
    return "{} B".format(size)


def _step(title: str, ok: bool, detail: str, level: str = "") -> dict:
    return {"title": title, "ok": bool(ok), "detail": detail, "level": level}


def _is_self_dir(path: str) -> bool:
    """这是不是本进程自己的解压目录（正在用，动不得）。"""
    mine = getattr(sys, "_MEIPASS", "") or ""
    if not mine:
        return False
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(
        os.path.abspath(mine))


def _short_path(path: str) -> str:
    """8.3 短路径（纯 ASCII），让批处理不受中文编码影响。

    取不到就原样返回 —— 批处理本身是按 GBK 写的，中文路径照样能认。
    """
    if os.name != "nt":
        return path
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
                                               wintypes.DWORD]
        kernel32.GetShortPathNameW.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.GetShortPathNameW(path, buf, 1024) and buf.value:
            return buf.value
    except Exception:                # noqa: BLE001 - 拿不到就算了，有 GBK 兜底
        pass
    return path


def _write_cmd(path: str, lines: list) -> None:
    """按系统 ANSI（中文机器 = GBK）编码 + CRLF 写批处理。

    这是踩过坑的：UTF-8 无 BOM 的脚本被 cmd 按 GBK 读，中文路径成乱码，
    整条命令就指到不存在的文件上去了。
    """
    data = "\r\n".join(lines) + "\r\n"
    with open(path, "wb") as fh:
        fh.write(data.encode("gbk", "replace"))


# ------------------------------------------------------------------ 注册表
def _read_run() -> str:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return str(winreg.QueryValueEx(key, AUTOSTART_VALUE)[0] or "")
    except OSError:
        return ""


def _approved_state() -> int:
    """任务管理器里的启用状态：0 没记录、2 启用、3 被禁用。"""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            raw = winreg.QueryValueEx(key, AUTOSTART_VALUE)[0]
    except OSError:
        return 0
    if isinstance(raw, (bytes, bytearray)) and raw:
        return int(raw[0])
    return 0


def _del_reg(key_path: str, name: str) -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        return True
    except OSError:
        return False                   # 本来就没这一项，不算失败


def _watch_running() -> bool:
    from campusnet import singleton

    try:
        return bool(singleton.is_taken(WATCH_LOCK))
    except Exception:                # noqa: BLE001
        return False


def _mei_ledger() -> str:
    return os.path.join(shell.config_dir(), "mei_ledger.txt")


def _looks_like_install_dir(pdir: str) -> bool:
    """这目录里有没有主程序 —— 有才敢动文件。

    卸载器正常情况下和 IL AUCT.exe 待在同一个目录。要是旁边没有主程序，
    说明用户单独把卸载器拷出来了（或者主程序早就被他删了），这时目录里
    那个 app.py / web 没准是别人项目的东西 —— 一律不碰，只清自启和守护。
    """
    return any(os.path.isfile(os.path.join(pdir, n))
               for n in ("IL AUCT.exe", "校园网助手.exe"))


def _temp_leftovers() -> list:
    """账本上记着的、以前没删干净的临时解压目录。"""
    try:
        with open(_mei_ledger(), encoding="utf-8") as fh:
            rows = [line.strip() for line in fh if line.strip()]
    except OSError:
        return []
    return [p for p in rows if os.path.isdir(p) and not _is_self_dir(p)]


# ------------------------------------------------------------------ 清单
def scan() -> dict:
    """卸载前先看一眼：有哪些东西、多大、当前什么状态。"""
    pdir = _program_dir()

    files = []
    for name in OWN_FILES + OWN_SOURCES:
        path = os.path.join(pdir, name)
        if os.path.isfile(path) and not _is_self(path):
            files.append({"name": name, "size": os.path.getsize(path)})

    dirs = []
    for name in OWN_DIRS:
        path = os.path.join(pdir, name)
        if os.path.isdir(path):
            dirs.append({"name": name, "size": _dir_size(path)})

    # web 只算我们自己那几个文件占的地方，不算整个目录
    web = os.path.join(pdir, "web")
    if os.path.isdir(web):
        web_size = sum(os.path.getsize(os.path.join(web, n))
                       for n in WEB_FILES if os.path.isfile(os.path.join(web, n)))
        if web_size:
            dirs.append({"name": "web", "size": web_size})

    program_size = sum(f["size"] for f in files) + sum(d["size"] for d in dirs)

    # 目录里不属于本程序的东西：只报告，绝不碰
    known = {n.lower() for n in OWN_FILES + OWN_SOURCES + OWN_DIRS}
    known |= {n.lower() for n in WEB_FILES}
    known |= {n.lower() for n in SELF_NAMES}
    known.add("web")
    me = os.path.basename(_frozen_exe()).lower()
    keep = []
    try:
        for entry in sorted(os.listdir(pdir)):
            low = entry.lower()
            if low in known or low == me:
                continue
            keep.append(entry)
    except OSError:
        pass

    cfg_dir = shell.config_dir()
    cfg_file = os.path.join(cfg_dir, "config.json")
    logs = [os.path.join(cfg_dir, n) for n in ("watch.log", "watch.log.old")]
    logs = [p for p in logs if os.path.isfile(p)]
    leftovers = _temp_leftovers()
    local_dir = _local_app_dir()
    legacy = [p for p in shell._legacy_startup_files() if os.path.exists(p)]

    # 一共能腾出多少地方（含那些默认保留的可选删除项，界面顶部用）
    total = (program_size
             + sum(_dir_size(p) for p in leftovers)
             + (_dir_size(local_dir) if os.path.isdir(local_dir) else 0)
             + (os.path.getsize(cfg_file) if os.path.isfile(cfg_file) else 0)
             + sum(os.path.getsize(p) for p in logs))

    return {
        "ok": True,
        "program_dir": pdir,
        "is_install_dir": _looks_like_install_dir(pdir),
        "total_size_text": _mb(total),
        # —— 必删
        "program_files": files,
        "program_dirs": dirs,
        "program_size_text": _mb(program_size),
        "program_count": len(files) + len(dirs),
        "keep_files": keep,
        "watch_running": _watch_running(),
        "autostart": bool(_read_run()),
        "autostart_command": _read_run(),
        "autostart_disabled": _approved_state() == 3,
        "legacy_files": [os.path.basename(p) for p in legacy],
        "temp_leftovers": [os.path.basename(p) for p in leftovers],
        "temp_size_text": _mb(sum(_dir_size(p) for p in leftovers)),
        "local_exists": os.path.isdir(local_dir),
        "local_size_text": _mb(_dir_size(local_dir)) if os.path.isdir(local_dir) else "0 B",
        # —— 可选删
        "config_file": cfg_file,
        "config_exists": os.path.isfile(cfg_file),
        "config_size_text": _mb(os.path.getsize(cfg_file)) if os.path.isfile(cfg_file) else "0 B",
        "log_files": [os.path.basename(p) for p in logs],
        "log_size_text": _mb(sum(os.path.getsize(p) for p in logs)),
        "log_exists": bool(logs),
        "version": str(getattr(shell, "__version__", "") or ""),
    }


# ------------------------------------------------------------------ 执行
def _stop_watch() -> dict:
    from campusnet import singleton

    try:
        if not singleton.is_taken(WATCH_LOCK):
            return _step("停止后台抢网", True, "本来就没在运行")
        if not singleton.signal_stop():
            return _step("停止后台抢网", False, "停止信号没能发出去", "warn")
    except Exception as exc:         # noqa: BLE001
        return _step("停止后台抢网", False, "出错了：{}".format(exc), "warn")

    for _ in range(200):             # 最多等 20 秒
        if not singleton.is_taken(WATCH_LOCK):
            return _step("停止后台抢网", True, "已退出")
        time.sleep(0.1)
    return _step("停止后台抢网", True,
                 "信号已送达，它正卡在一轮探测里，退干净还要几秒", "warn")


def _del_scheduled_task(name: str = TASK_NAME) -> bool:
    """删掉「开机加速」装的登录计划任务。

    这个任务是用管理员权限注册的，所以普通权限下删不掉 —— 删不掉就返回
    ``False``，由界面提示用户用「取消开机加速.bat」处理。**不报错**：
    它只是个加速开关，没装过也完全正常。
    """
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        done = subprocess.run(["schtasks", "/delete", "/tn", name, "/f"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=flags, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def _clear_autostart() -> dict:
    gone = []
    if _del_reg(RUN_KEY, AUTOSTART_VALUE):
        gone.append("注册表启动项")
    if _del_reg(APPROVED_KEY, AUTOSTART_VALUE):
        gone.append("任务管理器里的禁用记录")
    if _del_scheduled_task():
        gone.append("登录计划任务")
    if not gone:
        return _step("移除开机自启", True, "本来就没有自启项")
    return _step("移除开机自启", True, "已清掉 " + "、".join(gone))


def _clear_legacy() -> dict:
    gone = []
    for path in shell._legacy_startup_files():
        if not os.path.exists(path):
            continue
        try:
            os.remove(path)
            gone.append(os.path.basename(path))
        except OSError:
            pass
    if not gone:
        return _step("清理旧版启动脚本", True, "没有遗留文件")
    return _step("清理旧版启动脚本", True, "已删除 " + "、".join(gone))


def _clear_temp() -> dict:
    folders = _temp_leftovers()
    if not folders:
        try:
            os.remove(_mei_ledger())
        except OSError:
            pass
        return _step("清理临时解压目录", True, "没有残留")

    freed = 0
    left = 0
    for path in folders:
        freed += _dir_size(path)
        shutil.rmtree(path, ignore_errors=True)
        if os.path.isdir(path):
            left += 1
    try:
        os.remove(_mei_ledger())
    except OSError:
        pass
    detail = "清掉 {} 个目录，约 {}".format(len(folders) - left, _mb(freed))
    if left:
        return _step("清理临时解压目录", True,
                     detail + "；还有 {} 个正被占用，重启后会自己消失".format(left), "warn")
    return _step("清理临时解压目录", True, detail)


def _clear_config() -> dict:
    path = os.path.join(shell.config_dir(), "config.json")
    if not os.path.isfile(path):
        return _step("删除账号密码与配置", True, "config.json 不存在")
    try:
        os.remove(path)
        return _step("删除账号密码与配置", True, "已删除 config.json（里面的明文密码一并清除）")
    except OSError as exc:
        return _step("删除账号密码与配置", False, "删不掉：{}".format(exc), "warn")


def _clear_logs() -> dict:
    gone = []
    for name in ("watch.log", "watch.log.old"):
        path = os.path.join(shell.config_dir(), name)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            gone.append(name)
        except OSError:
            pass
    if not gone:
        return _step("删除运行日志", True, "没有日志文件")
    return _step("删除运行日志", True, "已删除 " + "、".join(gone))


def _clear_files() -> dict:
    pdir = _program_dir()

    # 先确认这真是本程序的安装目录 —— 别把别人的项目当成自己的家拆了
    if not _looks_like_install_dir(pdir):
        return _step("删除程序文件", True,
                     "这个目录里没有 IL AUCT.exe，像是卸载器被单独拿出来了 —— "
                     "为免误删别的东西，文件一个没动", "warn")

    removed, failed = [], []
    size = 0
    web_kept = False

    for name in OWN_FILES + OWN_SOURCES:
        path = os.path.join(pdir, name)
        if not os.path.isfile(path) or _is_self(path):
            continue                 # 自己留给最后那个批处理
        size += os.path.getsize(path)
        try:
            os.remove(path)
            removed.append(name)
        except OSError:
            failed.append(name)

    # web 目录只删我们自己那几个文件；里面要是还有别的东西，目录就留着
    web = os.path.join(pdir, "web")
    if os.path.isdir(web):
        for name in WEB_FILES:
            path = os.path.join(web, name)
            if not os.path.isfile(path):
                continue
            size += os.path.getsize(path)
            try:
                os.remove(path)
                removed.append("web/" + name)
            except OSError:
                failed.append("web/" + name)
        try:
            os.rmdir(web)
            removed.append("web/")
        except OSError:
            web_kept = True

    for name in OWN_DIRS:
        path = os.path.join(pdir, name)
        if not os.path.isdir(path):
            continue
        size += _dir_size(path)
        shutil.rmtree(path, ignore_errors=True)
        if os.path.isdir(path):
            failed.append(name)
        else:
            removed.append(name + "/")

    if not removed and not failed:
        return _step("删除程序文件", True, "没找到程序文件（可能之前已经删过了）", "warn")

    detail = "已删除 {} 项，约 {}".format(len(removed), _mb(size))
    if web_kept:
        detail += "；web 目录里还有别的东西，目录本身没动"
    if failed:
        return _step("删除程序文件", False,
                     "{}；{} 项没删掉（多半正被占用）".format(detail, len(failed)), "warn")
    return _step("删除程序文件", True, detail)


def _schedule_selfdestruct() -> dict:
    """把自己删掉 —— 运行中的 exe 删不了，交给退出后的批处理。"""
    exe = _frozen_exe()
    if not exe:
        return _step("删除卸载器自身", True, "开发模式，跳过")

    pdir = _program_dir()
    local_dir = _local_app_dir()
    script = os.path.join(tempfile.gettempdir(),
                          "ilauct_cleanup_{}.cmd".format(os.getpid()))

    del_exe = _short_path(exe)
    lines = [
        "@echo off",
        "setlocal",
        "rem 等主进程彻底退出再动手；最多重试 6 轮，每轮 2 秒",
        "set n=0",
        ":wait",
        "ping -n 3 127.0.0.1 >nul",
        'del /f /q "{}" >nul 2>nul'.format(del_exe),
        'if not exist "{}" goto gone'.format(del_exe),
        "set /a n+=1",
        "if %n% LSS 6 goto wait",
        ":gone",
        # /q 不带 /s：目录里还有你自己放的文件时它会失败，正好不误删
        'rd /q "{}" >nul 2>nul'.format(_short_path(pdir)),
    ]
    if os.path.isdir(local_dir):
        short_local = _short_path(local_dir)
        # 给 Edge 几秒钟退干净 —— 它刚被主进程关掉，缓存目录这会儿可能还占着；
        # 删不掉就再等一轮重试（这目录上过百兆，清干净很划算）
        lines.append("rem 等 Edge 退干净再清它的缓存，删不掉就再试一次")
        lines.append("ping -n 5 127.0.0.1 >nul")
        lines.append('rd /s /q "{}" >nul 2>nul'.format(short_local))
        lines.append('if exist "{}" ('.format(short_local))
        lines.append("ping -n 5 127.0.0.1 >nul")
        lines.append('rd /s /q "{}" >nul 2>nul'.format(short_local))
        lines.append(")")
    lines.append('del /f /q "%~f0" >nul 2>nul')

    try:
        _write_cmd(script, lines)
        subprocess.Popen(
            ["cmd", "/c", script],
            creationflags=_DETACHED | _NO_WINDOW,
            close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:         # noqa: BLE001
        return _step("删除卸载器自身", False,
                     "安排清理失败：{}；请手动删掉「{}」".format(
                         exc, os.path.basename(exe)), "warn")

    return _step("删除卸载器自身", True, "窗口关掉后自动完成（约几秒）")


def _do_uninstall(remove_config: bool, remove_logs: bool) -> dict:
    steps = [
        _stop_watch(),
        _clear_autostart(),
        _clear_legacy(),
        _clear_temp(),
    ]
    if remove_config:
        steps.append(_clear_config())
    if remove_logs:
        steps.append(_clear_logs())

    steps.append(_clear_files())

    # 配置和日志都删了的话，把空掉的配置目录也收走
    if remove_config and remove_logs:
        try:
            os.rmdir(shell.config_dir())
        except OSError:
            pass                 # 里面还有别的东西就留着，不硬来

    steps.append(_schedule_selfdestruct())

    return {
        "ok": True,
        "steps": steps,
        "failed": sum(1 for s in steps if not s["ok"]),
        "config_removed": remove_config,
        "logs_removed": remove_logs,
    }


# ------------------------------------------------------------------ 接口
class Api:
    """和主程序同一套 /api/<方法名> 约定，前端直接 fetch。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False

    def scan(self) -> dict:
        try:
            return scan()
        except Exception as exc:     # noqa: BLE001
            return {"ok": False, "message": "读取现状失败：{}".format(exc)}

    def run(self, remove_config: bool = False, remove_logs: bool = False) -> dict:
        with self._lock:
            if self._busy:
                return {"ok": False, "message": "正在卸载，请稍候"}
            self._busy = True
        try:
            return _do_uninstall(bool(remove_config), bool(remove_logs))
        except Exception as exc:     # noqa: BLE001
            return {"ok": False, "message": "卸载失败：{}".format(exc)}
        finally:
            with self._lock:
                self._busy = False


# ------------------------------------------------------------------ 启动
def run_gui() -> int:
    web_root = os.path.realpath(os.path.join(shell._base_dir(), "web"))
    index = os.path.join(web_root, "uninstall.html")
    if not os.path.exists(index):
        shell._fallback("找不到卸载界面文件：{}".format(index))
        return 1

    edge = shell._find_edge()
    if not edge:
        shell._fallback("没有找到 Microsoft Edge，界面打不开。\n\n"
                        "可以直接把程序目录整个删掉，效果一样 —— 记得先在"
                        "任务管理器里把开机启动项禁掉。")
        return 1

    api = Api()
    port = shell._free_port()
    handler = shell._Handler
    handler.api = api
    handler.web_root = web_root
    handler.index_name = "uninstall.html"
    handler.quit_flag = False
    handler.last_seen = time.time()

    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = "http://127.0.0.1:{}/uninstall.html".format(port)
    cmd = [
        edge,
        "--app=" + url,
        "--user-data-dir=" + shell._edge_profile(),
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,EdgeCollections",
        "--window-size=800,880",
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(cmd, creationflags=flags)
    except Exception as exc:         # noqa: BLE001
        shell._fallback("启动 Edge 失败：{}".format(exc))
        return 1

    started = time.time()
    proc_usable = True
    try:
        while not handler.quit_flag:
            if proc.poll() is not None:
                if time.time() - started < 8:
                    proc_usable = False     # 命令被转交给已有实例了，别误判
                elif proc_usable:
                    break
            if time.time() - handler.last_seen > 600:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:        # noqa: BLE001
                pass
        try:
            server.shutdown()
        except Exception:            # noqa: BLE001
            pass
    return 0


def main() -> int:
    argv = list(sys.argv[1:])
    if argv and argv[0] == "scan":
        import json

        print(json.dumps(scan(), ensure_ascii=False, indent=2))
        return 0
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
