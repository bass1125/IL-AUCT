"""IL AUCT —— 校园网助手图形界面（本地 HTTP 服务 + 系统 Edge app 窗口）。

用法：

    双击 exe / python app.py          → 打开窗口
    IL AUCT.exe watch                 → 无界面守护（开机自启用）
    IL AUCT.exe doctor                → 透传给 campusnet 命令行

打包用 PyInstaller 跑根目录的 IL AUCT.spec。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 路径
_HERE = os.path.dirname(os.path.abspath(__file__))
_FROZEN = bool(getattr(sys, "frozen", False))

if not _FROZEN:
    # 开发模式：直接引用 campusnet 仓库（保持单一副本，改代码不用同步两份）。
    # 注意仓库克隆下来的目录名也叫 campusnet，所以路径是 .../campusnet-autologin/campusnet
    _REPO = os.path.join(os.path.dirname(_HERE), "campusnet-autologin", "campusnet")
    if os.path.isdir(_REPO) and _REPO not in sys.path:
        sys.path.insert(0, _REPO)


def _base_dir() -> str:
    """资源目录（打包后是解包出来的临时目录）。"""
    if _FROZEN:
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return _HERE


APP_TITLE = "IL AUCT"

#: IL AUCT 自己的版本号 —— 跟底层 campusnet 的 ``__version__`` 是两回事。
#: 发新 Release 时改这里，tag 用 ``v`` + 这个号（如 v1.0.1）。
APP_VERSION = "1.0.0"

WATCH_INTERVAL = 3          # 守护检查间隔（分钟）
AUTOSTART_KEY = "campusnet"  # 注册表 Run 键下的值名（与上游一致）


# ---------------------------------------------------------------- stdio 兜底
def _fix_stdio() -> None:
    """--windowed 打包的进程里 stdout / stderr 是 None，print 会直接崩。"""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


_fix_stdio()

from campusnet import __version__, autostart, popup, singleton, wifi  # noqa: E402
from campusnet.cli import main as cli_main  # noqa: E402
from campusnet.config import Config, config_dir, default_config_path  # noqa: E402
from campusnet.detector import check_online, detect  # noqa: E402
from campusnet.runner import Runner  # noqa: E402
from campusnet.session import Session, local_ip  # noqa: E402

import updater  # noqa: E402

LOG_DIR = config_dir()
WATCH_LOG = os.path.join(LOG_DIR, "watch.log")


# ---------------------------------------------------------------- 日志
_LOG_LOCK = threading.Lock()

#: debug 级日志（每个 HTTP 请求两行）默认不落盘 —— 守护每 3 分钟跑一轮，
#: 一轮就写十来行"GET / 200"，纯粹是噪音。要排查时设 CN_ASSISTANT_DEBUG=1 开回来。
_VERBOSE_LOG = bool(os.environ.get("CN_ASSISTANT_DEBUG"))

#: 单个日志文件的上限，超了就在下次启动时轮转
LOG_MAX_BYTES = 2 * 1024 * 1024


def log(message: str, level: str = "info") -> None:
    """往 watch.log 追加一行（带时间戳）。"""
    if level == "debug" and not _VERBOSE_LOG:
        return
    mark = {"ok": "✔", "warn": "!", "error": "✘", "debug": "·"}.get(level, "•")
    line = "{} {} {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), mark, message)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with _LOG_LOCK, open(WATCH_LOG, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def rotate_log(max_bytes: int = LOG_MAX_BYTES) -> None:
    """日志超过上限就轮转一次（旧内容留成 ``watch.log.old``）。

    **只在启动时调用**：运行中 stdout 可能正重定向在这个文件上，
    Windows 不允许替换一个被打开的文件。
    """
    try:
        if os.path.getsize(WATCH_LOG) < max_bytes:
            return
    except OSError:
        return
    old = WATCH_LOG + ".old"
    try:
        if os.path.exists(old):
            os.remove(old)
        os.replace(WATCH_LOG, old)
    except OSError:
        pass


def read_log_lines(limit: int = 400) -> str:
    try:
        with open(WATCH_LOG, "r", encoding="utf-8", errors="replace") as fh:
            rows = fh.read().splitlines()
    except OSError:
        return ""
    return "\n".join(rows[-limit:])


def log_tail(n: int = 5) -> list:
    rows = [r.strip() for r in read_log_lines(300).splitlines() if r.strip()]
    out = []
    for row in rows:
        # 去掉时间戳，只留符号 + 内容，前端好排版
        body = re.sub(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*", "", row)
        if body and not re.fullmatch(r"[─━\-=\s]+", body):
            out.append(body)
    return out[-n:]


class _StampedStream:
    """给守护进程 print 出来的每一行补上时间戳。

    守护的核心输出（"守护模式启动" / "热身阶段已联网" / "本轮检查耗时 …"）
    走的是 ``print``，不是 :func:`log`，落到文件里只有顺序、没有时刻 ——
    「开机两分钟到底卡在哪一步」这种问题就完全没法回答。
    这里按行加前缀，`:func:`log_tail` 那边本来就会把前缀剥掉，界面不受影响。
    """

    def __init__(self, handle) -> None:
        self._handle = handle
        self._pending = ""

    def write(self, text) -> int:
        if not text:
            return 0
        self._pending += text
        while "\n" in self._pending:
            line, _, self._pending = self._pending.partition("\n")
            if line.strip():
                self._handle.write("{} {}\n".format(
                    time.strftime("%Y-%m-%d %H:%M:%S"), line))
            else:
                self._handle.write("\n")
        return len(text)

    def flush(self) -> None:
        try:
            self._handle.flush()
        except Exception:            # noqa: BLE001 - 日志写不了也不能崩
            pass

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        """必须暴露这个 —— 否则日志里所有符号都会被打成 ASCII 替代品。

        ``cli.Console`` 靠 ``getattr(stream, "encoding", None) or "ascii"`` 判断
        "这个流编不编得出中文和 ✔"，判断结果决定要不要整体降级成 ``v`` / ``-``。
        包了一层之后属性不见了，它就会以为编不出，日志立刻变得又丑又难读。
        """
        return getattr(self._handle, "encoding", None) or "utf-8"


def _redirect_std_to_log() -> None:
    """把 stdout / stderr 接到日志文件（守护模式用）。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        pass
    rotate_log()                 # 必须在打开句柄之前，否则 Windows 换不动
    try:
        fh = open(WATCH_LOG, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        return
    stream = _StampedStream(fh)
    sys.stdout = stream
    sys.stderr = stream


# ---------------------------------------------------------------- 后台探测
class Prober:
    """后台线程定期探测网络状态 —— 前台只读缓存，避免界面卡顿。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = {
            "online": False,
            "ip": "",
            "server": "",
            "detail": "正在检测…",
            "checked": 0.0,
        }
        self._stop = False

    def start(self) -> None:
        self._prime()
        threading.Thread(target=self._loop, daemon=True).start()

    def _prime(self) -> None:
        """先把本地就能拿到的信息（IP）填上，别让界面空着等网络探测。"""
        try:
            ip = local_ip() or ""
        except Exception:        # noqa: BLE001
            ip = ""
        with self._lock:
            self._data["ip"] = ip

    def _loop(self) -> None:
        time.sleep(0.8)          # 先让窗口画出来
        while not self._stop:
            try:
                self._once()
            except Exception:    # noqa: BLE001 - 探测失败不该影响界面
                pass
            time.sleep(12)

    def _once(self) -> None:
        cfg = Config.load()
        session = Session(timeout=4, use_proxy=cfg.use_proxy)
        status = check_online(session, cfg.portal_ip or "")

        server = ""
        with self._lock:
            server = self._data.get("server", "")
        if not server:
            try:
                server = detect(session, cfg, status).server or ""
            except Exception:    # noqa: BLE001
                pass

        with self._lock:
            self._data.update({
                "online": bool(status.online),
                "ip": local_ip() or "",
                "server": server or (cfg.portal_ip or ""),
                "detail": status.describe(),
                "checked": time.time(),
            })

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._data)

    def force(self) -> None:
        try:
            self._once()
        except Exception:        # noqa: BLE001
            pass


# ---------------------------------------------------------------- 守护
# 定位：这个窗口是「设置器」，不是「自启器」。
# 用户在界面里填一次账号密码、勾上开机自动连接，之后开机联网全靠后台的
# 静默守护进程；窗口本身既不随开机弹出，也不需要在联网期间一直开着。
# 所以守护跑在**独立进程**里，界面只负责把它拉起来 / 查状态 / 叫停。
_DETACHED_PROCESS = 0x00000008

# PyInstaller 单文件模式的内部环境变量。子进程要是继承了它们，就会**复用父进程
# 解压出来的那个临时目录**（%TEMP%\_MEIxxxxxx），而不是自己解压一份 —— 这样父进程
# 关窗口退出时，那个目录里的 python313.dll 还被守护占着，删不掉，于是弹一个
# 「Failed to remove temporary directory」的 Warning，而且 Temp 里越堆越多。
# 所以拉守护之前先把这套变量摘干净，让它独立解压、独立清理。
_PYI_ENV_KEYS = (
    "_MEIPASS", "_MEIPASS2",
    "_PYI_APPLICATION_HOME_DIR", "_PYI_PARENT_PROCESS_LEVEL",
    "_PYI_ARCHIVE_FILE", "_PYI_SPLASH_IPC",
)


def _child_env() -> dict:
    """给子进程一份「不是从 PyInstaller 里生出来的」环境。"""
    env = os.environ.copy()
    for key in _PYI_ENV_KEYS:
        env.pop(key, None)
    return env


def _spawn_watch() -> bool:
    """在后台拉起一个无窗口的静默守护进程。

    关键点：进程要**脱离**界面进程，否则关窗口时会被一起带走 ——
    那就又回到"必须一直开着界面才有守护"的老路了。
    """
    if _FROZEN:
        cmd = [sys.executable, "watch", "--interval", str(WATCH_INTERVAL)]
    else:
        # 开发模式：用 pythonw.exe 跑，免得蹦出一个黑框
        python = sys.executable or "python"
        quiet = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.exists(quiet):
            python = quiet
        cmd = [python, os.path.join(_HERE, "app.py"),
               "watch", "--interval", str(WATCH_INTERVAL)]

    cfg_path = default_config_path()
    if cfg_path:
        cmd += ["--config", cfg_path]

    flags = _DETACHED_PROCESS if os.name == "nt" else 0
    try:
        subprocess.Popen(
            cmd,
            creationflags=flags,
            close_fds=True,
            env=_child_env(),      # 见 _PYI_ENV_KEYS：不然会跟本窗口抢临时目录
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:        # noqa: BLE001
        log("拉起后台守护失败：{}".format(exc), "error")
        return False
    log("已在后台启动静默守护（关掉本窗口也不影响它）", "ok")
    return True


class Watcher:
    """后台守护的「遥控器」：查状态 / 拉起 / 叫停，自己不跑守护。"""

    def ensure(self) -> bool:
        """没在跑就拉起一个静默守护；返回是否真的拉起了。"""
        if self.running():
            return False
        if not _spawn_watch():
            return False
        # 给它一点时间拿锁，免得界面上还显示"未运行"
        for _ in range(25):
            time.sleep(0.1)
            if self.running():
                break
        return True

    def ensure_if_configured(self) -> None:
        """只在用户开过「开机自动连接」时才保证后台有守护在跑。"""
        try:
            if not autostart_enabled():
                return
            if not Config.load().username:
                return
        except Exception:            # noqa: BLE001
            return
        self.ensure()

    def running(self) -> bool:
        """锁被占着 = 有守护在跑。"""
        return not _lock_free()

    @staticmethod
    def stop() -> bool:
        """叫停正在跑的守护（取消「开机自动连接」时用）。"""
        return singleton.signal_stop()


def _lock_free() -> bool:
    """锁没被占用 = 没有守护在跑。

    用只读探测（``is_taken``）而不是"抢一次再还回去"：后者每查询一次
    就要真的创建一个互斥体，万一哪次句柄没还干净，本进程就会把锁
    "撑"成永远占用 —— 界面从此一直显示「守护：运行中」。
    """
    return not singleton.is_taken("campusnet-watch")


# ---------------------------------------------------------------- 自启
def _startup_dir() -> str:
    appdata = os.environ.get("APPDATA") or ""
    if not appdata:
        return ""
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup")


def _legacy_startup_files() -> list:
    """「启动」文件夹里可能存在的自启文件（早期脚本版 / 快捷方式版）。"""
    folder = _startup_dir()
    if not folder:
        return []
    names = ("校园网自动登录.vbs", "校园网自动登录.lnk", "校园网自动登录.cmd",
             "campusnet.vbs", "campusnet.cmd", "start-campusnet.vbs")
    return [os.path.join(folder, n) for n in names]


def autostart_enabled() -> bool:
    """自启现状：程序自己注册的（exe）或早期脚本放的，任一生效都算开。"""
    try:
        if "未安装" not in autostart.status():
            return True
    except Exception:            # noqa: BLE001
        pass
    return any(os.path.exists(p) for p in _legacy_startup_files())


# ---------------------------------------------------------------- 开机加速
# 注册表 Run 项 Windows 要等桌面铺好才轮到（实测桌面出现到守护起来差 20 秒），
# 而「登录时」计划任务在登录瞬间就触发，能把这段排队省掉。
# 代价：装和卸都要管理员权限（会弹一次 UAC），系统里多一个计划任务
#（卸载器会顺手清掉），以及守护可能被拉起两次 —— 有单实例锁，第二次秒退。
FASTBOOT_TASK_NAME = "IL AUCT Autologin"
_FASTBOOT_TASK_FILE = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32", "Tasks", FASTBOOT_TASK_NAME)


def fastboot_installed() -> bool:
    """计划任务在不在。

    直接 stat ``System32\\Tasks`` 下的任务定义文件 —— 单个文件查得到
    （列不了目录，但不影响），所以这里是零开销的，不用起 PowerShell。
    """
    try:
        return os.path.isfile(_FASTBOOT_TASK_FILE)
    except OSError:
        return False


def _ps_quote(value: str) -> str:
    """包成 PowerShell 单引号字符串（里面的单引号翻倍）。"""
    return "'" + str(value).replace("'", "''") + "'"


def _fastboot_script(enable: bool) -> str:
    """提权之后要跑的 PowerShell 脚本。"""
    if not enable:
        return ("$ErrorActionPreference = 'SilentlyContinue'\n"
                "Unregister-ScheduledTask -TaskName {name} -Confirm:$false "
                "-ErrorAction SilentlyContinue\n"
                "exit 0\n").format(name=_ps_quote(FASTBOOT_TASK_NAME))

    cfg_path = Config.load().path or default_config_path()
    arguments = 'watch --interval {} --config "{}"'.format(WATCH_INTERVAL, cfg_path)
    return (
        "$ErrorActionPreference = 'Stop'\n"
        "$sid = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value\n"
        "$action = New-ScheduledTaskAction -Execute {exe} -Argument {args}\n"
        "$trigger = New-ScheduledTaskTrigger -AtLogOn\n"
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-MultipleInstances IgnoreNew\n"
        "$principal = New-ScheduledTaskPrincipal -UserId $sid "
        "-LogonType Interactive -RunLevel Highest\n"
        "Register-ScheduledTask -TaskName {name} -Action $action -Trigger $trigger "
        "-Settings $settings -Principal $principal -Force -ErrorAction Stop "
        "| Out-Null\n"
        "exit 0\n"
    ).format(exe=_ps_quote(os.path.abspath(sys.executable)),
             args=_ps_quote(arguments),
             name=_ps_quote(FASTBOOT_TASK_NAME))


def _run_elevated(exe: str, params: str, timeout: float = 240.0) -> tuple:
    """以管理员身份启动一个进程（会弹 UAC），并等它跑完。

    返回 ``(state, detail)``，state 取值：

    ``ok``         跑完了且退出码为 0
    ``cancelled``  用户在 UAC 上点了「否」
    ``timeout``    等超时（UAC 窗口还挂着，或脚本卡住了）
    ``error``      别的失败
    """
    if os.name != "nt":
        return "error", "只有 Windows 支持"

    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SW_HIDE = 0
    WAIT_TIMEOUT = 258

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = params
    info.nShow = SW_HIDE

    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == 1223:                       # ERROR_CANCELLED
            return "cancelled", "用户取消了授权"
        return "error", "没能启动授权进程（错误码 {}）".format(code)

    handle = info.hProcess
    if not handle:
        return "error", "没拿到进程句柄"
    try:
        if kernel32.WaitForSingleObject(handle, int(max(1.0, timeout) * 1000)) == WAIT_TIMEOUT:
            return "timeout", "等待授权超时"
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return "error", "读不到退出码"
        if exit_code.value == 0:
            return "ok", ""
        return "error", "提权进程退出码 {}".format(exit_code.value)
    finally:
        kernel32.CloseHandle(handle)


def _apply_fastboot(enable: bool) -> tuple:
    """装 / 卸「登录时」计划任务。返回 ``(ok, 给人看的一句话)``。"""
    if not _FROZEN:
        return False, "开发模式下不安装开机加速"
    if os.name != "nt":
        return False, "只有 Windows 支持开机加速"

    shell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                         "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if not os.path.isfile(shell):
        shell = "powershell.exe"

    encoded = base64.b64encode(_fastboot_script(enable).encode("utf-16-le")).decode("ascii")
    state, detail = _run_elevated(
        shell, "-NoProfile -ExecutionPolicy Bypass -EncodedCommand " + encoded)

    if state == "cancelled":
        return False, "你取消了管理员授权，开机加速没有改动"
    if state == "timeout":
        return False, "等待授权超时，开机加速没有改动"
    if state != "ok":
        return False, "开机加速设置失败：{}".format(detail)

    # 提权进程说成功还不够，回头看一眼任务到底在不在
    if fastboot_installed() != bool(enable):
        return False, "开机加速好像没生效，请再试一次"
    return True, ("已开启开机加速：登录瞬间就启动守护，比系统启动项早十几秒"
                  if enable else "已关闭开机加速，改回由系统启动项负责")


# ---------------------------------------------------------------- 前端接口
class Api:
    def __init__(self, prober: Prober, watcher: Watcher) -> None:
        self.prober = prober
        self.watcher = watcher
        self.window = None
        self.busy = False
        self.last_action = ""
        self._busy_lock = threading.Lock()

        # 更新状态（守护线程在后台写，界面读，所以统一用一把锁护着）
        self._update_lock = threading.Lock()
        self.update = {"ok": False, "available": False, "current": APP_VERSION,
                       "latest": "", "notes": "", "size": 0, "url": "",
                       "page": "", "error": "", "checked": False}
        self._download = {"running": False, "done": 0, "total": 0,
                          "ok": None, "error": ""}

    # ---------------- 状态
    def get_state(self) -> dict:
        cfg = Config.load()
        snap = self.prober.snapshot()
        return {
            "online": snap["online"],
            "busy": self.busy,
            "first_check_done": bool(snap["checked"]),
            "status_text": snap["detail"] if snap["checked"] else "正在检测…",
            "ip": snap["ip"],
            "server": snap["server"],
            "ssid": self._ssid(cfg),
            "last_action": self.last_action or ("刚刚检查" if snap["checked"] else "—"),
            "username": cfg.username or "",
            "wifi_ssid": cfg.wifi_ssid or "",
            "password_set": bool(cfg.password),
            "remember": bool(cfg.password),
            "autostart": autostart_enabled(),
            "fastboot": fastboot_installed(),
            "close_popup": popup.enabled(cfg.options),
            "watch_running": self.watcher.running(),
            "version": APP_VERSION,
            "provider": cfg.provider or "auto",
            "cfg_dir": config_dir(),
            "cfg_file": cfg.path or default_config_path(),
            "log_file": WATCH_LOG,
            "log_tail": log_tail(5),
            "update": self._update_snapshot(),
        }

    @staticmethod
    def _ssid(cfg) -> str:
        try:
            now = wifi.current_ssid()
        except Exception:        # noqa: BLE001
            now = ""
        if cfg.wifi_ssid:
            return "{}（当前 {}）".format(cfg.wifi_ssid, now or "未连接")
        return now or "—"

    def probe(self) -> dict:
        self.prober.force()
        return {"ok": True}

    # ---------------- 动作
    def connect_now(self) -> dict:
        if not self._begin():
            return {"ok": False, "message": "正在连接中，请稍候"}
        try:
            cfg = Config.load()
            if not cfg.username:
                return {"ok": False, "message": "还没填账号，先去「账号与自启」里填好"}
            if not cfg.password:
                return {"ok": False, "message": "还没填密码"}

            runner = Runner(cfg, logger=log)
            log("手动触发一次连接…")
            result = runner.ensure_online(patient=True, limit=2)
            self.last_action = time.strftime("%H:%M:%S") + " 手动连接"
            self.prober.force()
            return {"ok": bool(result.ok), "message": result.message or "已完成"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": "出错了：{}".format(exc)}
        finally:
            self._end()

    def save_settings(self, username: str, password: str, remember: bool,
                      autostart_on: bool, wifi_ssid: str = "",
                      close_popup: bool = True, fastboot_on: bool = True) -> dict:
        username = (username or "").strip()
        if not username:
            return {"ok": False, "message": "用户名不能为空"}

        try:
            cfg = Config.load()
            cfg.username = username
            # 留空表示"只做认证、不切网"—— 这在别人机器上很常见
            cfg.wifi_ssid = (wifi_ssid or "").strip()
            # 登录好后要不要顺手关掉 Windows 自己弹的那张登录页（见 campusnet.popup）
            cfg.options["close_portal_popup"] = bool(close_popup)

            if password:
                cfg.password = password
            if not remember:
                cfg.password = ""
                cfg.password_source = ""
            else:
                cfg.password_source = "config"

            path = cfg.save(include_password=bool(remember and cfg.password))
            log("配置已保存（账号 {}，密码{}）".format(
                username, "已记住" if remember else "不保存"), "ok")

            notes = []
            spawned = stopped = False
            if autostart_on:
                self._clear_legacy_vbs()
                notes.append(autostart.install(path, WATCH_INTERVAL))
                # 开机自启负责"以后"，这里负责"现在" —— 不然用户设置完
                # 关掉窗口，直到下次重启之前都没有东西在盯网络
                spawned = self.watcher.ensure()
            else:
                notes.append(autostart.uninstall())
                self._clear_legacy_vbs()
                # 当轮就跑着的守护也该一起收掉，别让用户以为点了没生效
                stopped = self.watcher.stop()
                if stopped:
                    log("已通知后台守护退出", "info")
                    # 守护可能正卡在一轮探测里（等网卡 / 提交认证），
                    # 那一轮跑完才会看信号，所以这里给它几秒钟退干净
                    for _ in range(30):
                        if not self.watcher.running():
                            break
                        time.sleep(0.1)

            self.prober.force()
            # 开机加速：只在状态真的变了才动手 —— 每次保存都弹一次 UAC 会把人烦死。
            # 没勾「开机自动连接」就没必要加速，这时候顺手当关处理。
            want_fast = bool(fastboot_on) and bool(autostart_on)
            if want_fast != fastboot_installed():
                fast_ok, fast_note = _apply_fastboot(want_fast)
                log(fast_note, "ok" if fast_ok else "warn")
                notes.append(fast_note)

            message = "已保存 · " + notes[0].splitlines()[0]
            if len(notes) > 1:
                message += "；" + notes[1]
            return {"ok": True,
                    "message": message,
                    "fastboot": fastboot_installed(),
                    "watch_started": spawned,
                    "watch_stopped": stopped}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": "保存失败：{}".format(exc)}

    def _begin(self) -> bool:
        with self._busy_lock:
            if self.busy:
                return False
            self.busy = True
            return True

    def _end(self) -> None:
        with self._busy_lock:
            self.busy = False

    def _clear_legacy_vbs(self) -> None:
        """清掉早期版本的启动文件夹脚本，免得两套自启同时跑。"""
        for path in _legacy_startup_files():
            if not os.path.exists(path):
                continue
            try:
                os.remove(path)
                log("已清理旧版启动项：{}（改由本程序接管自启）".format(
                    os.path.basename(path)), "warn")
            except OSError:
                pass

    # ---------------- 更新
    def _update_snapshot(self) -> dict:
        with self._update_lock:
            snap = dict(self.update)
            dl = dict(self._download)
        snap["downloading"] = bool(dl.get("running"))
        snap["downloaded"] = bool(dl.get("ok"))
        snap["progress"] = dl.get("done") or 0
        snap["progress_total"] = dl.get("total") or 0
        snap["download_error"] = dl.get("error") or ""
        return snap

    def check_update(self, silent: bool = False) -> dict:
        """查有没有新版本。

        ``silent=True`` 是开机后台自动跑的那次 —— 没问题就别往日志里写东西，
        也别让界面弹提示，安安静静查完就行。
        """
        info = updater.check(APP_VERSION)
        info["checked"] = True
        with self._update_lock:
            self.update = info
            # 换了新版本信息，之前下的包就不作数了
            if self._download.get("ok"):
                self._download = {"running": False, "done": 0, "total": 0,
                                  "ok": None, "error": ""}

        if info.get("available"):
            log("发现新版本 {}（当前 {}）".format(info["latest"], APP_VERSION), "ok")
        elif not silent:
            if info.get("ok"):
                log("已是最新版本（{}）".format(APP_VERSION), "info")
            else:
                log("检查更新失败：{}".format(info.get("error") or "未知原因"), "warn")
        return info

    def download_update(self) -> dict:
        """后台下载新版本，立即返回；进度靠 :meth:`update_progress` 轮询。"""
        with self._update_lock:
            if self._download.get("running"):
                return {"ok": False, "message": "正在下载中，请稍候"}
            info = dict(self.update)

        if not info.get("available"):
            return {"ok": False, "message": "当前没有可更新的版本"}
        if not info.get("url"):
            return {"ok": False, "message": "这个版本没有提供安装包"}

        dest = updater.staged_path()
        with self._update_lock:
            self._download = {"running": True, "done": 0,
                              "total": int(info.get("size") or 0),
                              "ok": None, "error": ""}

        def on_progress(done: int, total: int) -> None:
            with self._update_lock:
                self._download["done"] = done
                if total:
                    self._download["total"] = total

        def worker() -> None:
            ok, err = updater.download(info["url"], dest, on_progress)
            with self._update_lock:
                self._download["running"] = False
                self._download["ok"] = ok
                self._download["error"] = err
                done = self._download["done"]
            if ok:
                log("新版本已下载完成（{:.1f} MB），点「立即更新」生效".format(
                    done / 1048576.0), "ok")
            else:
                log("下载新版本失败：{}".format(err), "error")

        threading.Thread(target=worker, daemon=True).start()
        log("正在下载新版本 {}…".format(info.get("latest") or ""), "info")
        return {"ok": True, "started": True}

    def update_progress(self) -> dict:
        return self._update_snapshot()

    def apply_update(self) -> dict:
        """停掉守护 → 起替换脚本 → 让本程序退出。

        退出这一步必须的：Windows 上运行中的 exe 不能被覆盖，脚本要等我们
        彻底消失才能动手。
        """
        if not _FROZEN:
            return {"ok": False, "message": "开发模式请直接 git pull 更新"}

        with self._update_lock:
            finished = bool(self._download.get("ok"))
        if not finished:
            return {"ok": False, "message": "新版本还没下载好"}

        new_exe = updater.staged_path()
        if not os.path.exists(new_exe):
            return {"ok": False, "message": "安装包不见了，请重新下载一次"}

        # 守护进程也叫 IL AUCT.exe，不先叫停它，替换会一直重试到超时
        try:
            singleton.signal_stop()
        except Exception:            # noqa: BLE001
            pass
        for _ in range(40):
            if not self.watcher.running():
                break
            time.sleep(0.1)

        script = updater.write_script(sys.executable, new_exe)
        if not updater.launch(script):
            return {"ok": False, "message": "启动更新程序失败，请手动下载新版本"}

        log("正在退出以完成更新，稍后会自动重启…", "ok")
        _Handler.quit_flag = True
        return {"ok": True, "message": "更新即将开始，程序会自动重启"}

    # ---------------- 日志 / 目录
    def read_log(self, lines: int = 400) -> dict:
        return {"ok": True, "text": read_log_lines(int(lines or 400))}

    @staticmethod
    def _open(path: str) -> dict:
        if not path or not os.path.exists(path):
            return {"ok": False, "message": "路径不存在"}
        try:
            os.startfile(path)  # noqa: S606 - Windows 专用，打开资源管理器
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": str(exc)}

    def open_data_dir(self) -> dict:
        return self._open(config_dir())

    def open_log_file(self) -> dict:
        if not os.path.exists(WATCH_LOG):
            log("日志文件已创建", "info")
        return self._open(WATCH_LOG)

    def open_config_dir(self) -> dict:
        """打开配置/日志目录 —— 里面有 config.json 和 watch.log。

        （自启已经改用注册表 Run 项，不再往「启动」文件夹放脚本，
        所以那个目录没什么好看的，这里改成开更有用的一处。）
        """
        return self._open(LOG_DIR)

    # ---------------- 窗口
    def win_minimize(self) -> dict:
        if self.window:
            self.window.minimize()
        return {"ok": True}

    def win_close(self) -> dict:
        if self.window:
            self.window.destroy()
        return {"ok": True}


# ---------------------------------------------------------------- 窗口外壳
# 渲染内核和 AIAS 那类 Tauri 应用完全一样（Windows 上都是 WebView2 / Chromium），
# 区别只在外壳：这里用系统自带 Edge 的 app 模式，省掉 Python 经 .NET 桥接
# 调 WebView2 这一层（那层在这台机器上会因 COM apartment 而挂掉，表现为黑屏）。
EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge Beta\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge Dev\Application\msedge.exe",
)


def _find_edge() -> str:
    """定位 msedge.exe：先探常见安装位置，再查注册表 App Paths。"""
    for path in EDGE_PATHS:
        if os.path.exists(path):
            return path
    try:
        import winreg

        sub = r"Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
        keys = (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\\" + sub),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\" + sub),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\\" + sub),
        )
        for root, key in keys:
            try:
                with winreg.OpenKey(root, key) as handle:
                    found = winreg.QueryValueEx(handle, "")[0]
            except OSError:
                continue
            if found and os.path.exists(found):
                return found
    except Exception:                # noqa: BLE001
        pass
    return ""


def _edge_profile() -> str:
    """独立的浏览器配置目录 —— 不碰用户自己的 Edge 书签 / 登录态。"""
    base = (os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP")
            or tempfile.gettempdir())
    path = os.path.join(base, "CampusNetAssistant", "edge-profile")
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return tempfile.mkdtemp(prefix="cnassist-edge-")


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class _Handler(BaseHTTPRequestHandler):
    """把 /api/<方法名> 映射到 Api 的同名方法；其余路径当静态文件发。"""

    api = None
    web_root = ""
    index_name = "index.html"   # 卸载器复用本外壳时会改成 uninstall.html
    last_seen = 0.0
    quit_flag = False
    server_version = "CampusNetAssistant"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):    # 静音，别往控制台刷
        pass

    # ---------------- 工具
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    # ---------------- 路由
    def do_POST(self) -> None:
        path = self.path.split("?")[0]
        _Handler.last_seen = time.time()

        if path == "/api/quit":
            _Handler.quit_flag = True
            self._json({"ok": True})
            return
        if not path.startswith("/api/"):
            self._json({"ok": False, "message": "not found"}, 404)
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}
        args = payload.get("args") if isinstance(payload, dict) else None
        if not isinstance(args, list):
            args = []

        name = path[len("/api/"):]
        method = getattr(self.api, name, None) if name and not name.startswith("_") else None
        if not callable(method):
            self._json({"ok": False, "message": "未知接口：{}".format(name)}, 404)
            return
        try:
            result = method(*args)
        except Exception as exc:     # noqa: BLE001 - 任何异常都回给前端
            self._json({"ok": False, "message": str(exc)})
            return
        self._json(result if result is not None else {"ok": True})

    def do_GET(self) -> None:
        _Handler.last_seen = time.time()
        path = self.path.split("?")[0]
        if path == "/":
            path = "/" + self.index_name
        rel = path.lstrip("/").replace("\\", "/")
        full = os.path.realpath(os.path.join(self.web_root, rel))
        if not full.startswith(self.web_root) or not os.path.isfile(full):
            self._send(404, b"404 not found", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json"):
            ctype += "; charset=utf-8"
        try:
            with open(full, "rb") as handle:
                data = handle.read()
        except OSError:
            self._send(500, b"500 read error", "text/plain; charset=utf-8")
            return
        self._send(200, data, ctype)


# ---------------------------------------------------------------- 启动
def run_gui() -> int:
    rotate_log()
    # 放后台线程：清几十兆残留可能要几秒，不能让窗口干等着不出来
    threading.Thread(target=_sweep_stale_mei, daemon=True).start()
    web_root = os.path.realpath(os.path.join(_base_dir(), "web"))
    index = os.path.join(web_root, "index.html")
    if not os.path.exists(index):
        _fallback("找不到界面文件：{}".format(index))
        return 1

    edge = _find_edge()
    if not edge:
        _fallback("没有找到 Microsoft Edge，界面无法打开。\n\n"
                  "装好 Edge 再试，或先用命令行模式：\n"
                  "     IL AUCT.exe doctor")
        return 1

    prober = Prober()
    watcher = Watcher()
    api = Api(prober, watcher)
    prober.start()
    # 用户之前开过「开机自动连接」的话，保证后台现在也有一份守护在跑
    # （正常是从开机起就一直在跑；这里覆盖"守护挂过 / 手动关过"的情况）。
    # 放后台线程做：拉起并等它拿锁最多要 2 秒多，不能让窗口干等着不出来。
    threading.Thread(target=watcher.ensure_if_configured, daemon=True).start()

    # 静默查一次有没有新版本 —— 有就在界面上提示，没有就算了。
    # 放后台线程：网络慢的话不该拖着窗口不显示。
    threading.Thread(target=api.check_update, kwargs={"silent": True},
                     daemon=True).start()
    # 清掉上一次更新留下的残渣（新版本已经替换上去、脚本也跑完了的情况）
    threading.Thread(target=updater.cleanup, daemon=True).start()

    port = _free_port()
    _Handler.api = api
    _Handler.web_root = web_root
    _Handler.quit_flag = False
    _Handler.last_seen = time.time()

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = "http://127.0.0.1:{}/index.html".format(port)
    log("界面服务已就绪：{}".format(url), "ok")

    cmd = [
        edge,
        "--app=" + url,
        "--user-data-dir=" + _edge_profile(),
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,EdgeCollections",
        "--window-size=1180,800",
    ]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(cmd, creationflags=flags)
    except Exception as exc:         # noqa: BLE001
        _fallback("启动 Edge 失败：{}".format(exc))
        return 1

    # 退出判据三条，优先级从高到低：
    #   1) 页面 beforeunload 发的 /api/quit —— 正常的关窗路径，最准；
    #   2) Edge 进程消失 —— 窗口被强杀时兜底（独立 user-data-dir 保证不复用实例，
    #      所以这个进程就是窗口本身；启动 8 秒内就退出的除外，那说明命令被转交了）；
    #   3) 心跳超时 —— 最兜底，阈值放到 10 分钟。窗口被遮挡 / 最小化时 Chromium
    #      会把定时器节流到分钟级，阈值设小了会把还开着的窗口误判成已关闭。
    started = time.time()
    proc_usable = True
    try:
        while not _Handler.quit_flag:
            if proc.poll() is not None:
                if time.time() - started < 8:
                    proc_usable = False
                elif proc_usable:
                    log("界面窗口已关闭，退出", "info")
                    break
            if time.time() - _Handler.last_seen > 600:
                log("界面失联超过 10 分钟，退出", "info")
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


# ---------------------------------------------------------------- 临时目录兜底
# 单文件模式会把程序解压到 %TEMP%\_MEIxxxxxx，退出时再删掉。两种情况下删不掉、
# 于是堆在 Temp 里：
#   1. 子进程继承了 _PYI_* 环境变量、复用了父进程的临时目录 —— 父进程退出时
#      里面的 python313.dll 还被守护占着（就是那个 Warning 弹框的来历）；
#   2. 进程被强杀，压根没走到清理那一步。
# 第 1 条已经在 _spawn_watch 里从源头堵住了；这里是第 2 条的兜底：每次开窗口
# 顺手扫一遍自己的旧目录，能删的删掉，删不动的留到下次。
def _mei_ledger_path() -> str:
    """记账本：本程序自己解压出来的临时目录，都写在这。"""
    return os.path.join(config_dir(), "mei_ledger.txt")


def _sweep_stale_mei() -> None:
    """清理本程序以前留下的解压目录。

    只碰账本上记过的路径（= 本程序自己解压出来的），绝不误伤别人的程序。
    已经不在的、删不干净的（还有进程在用），一律原样留着，下次开窗口再说 ——
    所以不会出现「删一半把人家搞崩」这种事。
    """
    current = getattr(sys, "_MEIPASS", "") or ""
    ledger = _mei_ledger_path()
    try:
        with open(ledger, encoding="utf-8") as fh:
            recorded = [line.strip() for line in fh if line.strip()]
    except OSError:
        recorded = []

    pending = []
    freed = 0
    for folder in recorded:
        if os.path.normcase(folder) == os.path.normcase(current):
            continue
        if not os.path.isdir(folder):
            continue
        shutil.rmtree(folder, ignore_errors=True)
        if os.path.isdir(folder):
            pending.append(folder)     # 没删干净，留着下次再试
        else:
            freed += 1

    if current:
        pending.append(current)        # 记上自己，万一被强杀，下次开窗口能收尸
    try:
        os.makedirs(os.path.dirname(ledger), exist_ok=True)
        with open(ledger, "w", encoding="utf-8") as fh:
            fh.write("\n".join(dict.fromkeys(pending)) + "\n")
    except OSError:
        pass
    if freed:
        log("顺手清掉了 {} 个以前残留的临时目录".format(freed))


def _fallback(text: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, APP_TITLE, 0x10)
    except Exception:            # noqa: BLE001
        print(text)


def _cmd_update_check(argv: list) -> int:
    """查一次更新。

    打包版是 --windowed，没有控制台，所以支持把结果写进文件：
    ``IL AUCT.exe update-check D:\\out.json`` —— 排查「更新检查失败」时很方便。
    """
    info = updater.check(APP_VERSION)
    text = json.dumps(info, ensure_ascii=False, indent=2)
    target = argv[0] if argv else ""
    if target:
        try:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except OSError as exc:
            print("写入失败：{}".format(exc))
            return 1
    print(text)
    return 0


def main() -> int:
    argv = list(sys.argv[1:])

    if not argv:
        return run_gui()

    # 守护类命令把输出落到日志里（--windowed 的 exe 本来也看不到控制台）
    if argv[0] in ("watch", "silent", "--silent"):
        if argv[0] in ("silent", "--silent"):
            argv = ["watch", "--interval", str(WATCH_INTERVAL)]
        _redirect_std_to_log()
        return cli_main(argv)

    if argv[0] in ("update-check", "check-update"):
        return _cmd_update_check(argv[1:])

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
