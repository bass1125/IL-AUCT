"""watch 单实例锁：同一台机器同一时刻只允许一个守护进程。

为什么需要
----------

自启入口（Run 键）虽然只有一个，但 GUI 的「保存并立即连接」等路径也可能
拉起一个 watch。两个守护进程同时跑，探测动作翻倍不说，还会互相把对方
刚连好的 Wi-Fi 切走 —— 实测出现过两个 watcher 并存，开机黑窗跳得更多。

用一把**随进程死亡自动释放**的跨进程锁兜底：

- Windows：命名互斥体（内核对象，进程退出即销毁）；
- POSIX：锁文件 + ``flock``（进程死亡内核自动解锁，不会留尸体文件）。

抢不到锁的实例安静退出，不算错误。
"""

import os
import time

_HANDLE = None  # 持锁引用，防 GC / 防重复
_STOP_HANDLE = None  # 停止事件句柄（守护侧持有）
_K32 = None  # 带签名的 kernel32 缓存

#: 停止事件的命名 —— 界面取消「开机自动连接」时用它把守护叫停
STOP_NAME = "campusnet-watch-stop"


def _kernel32():
    """带正确签名的 kernel32。

    必须显式声明 restype/argtypes：HANDLE 是 64 位指针，ctypes 默认按
    ``c_int`` 处理会把它截断（这台机器上句柄恰好是小整数所以"看着能跑"，
    换台机器就可能出怪事）。签名只设一次，缓存在模块级。
    """
    global _K32
    if _K32 is None:
        import ctypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        k.OpenMutexW.restype = ctypes.c_void_p
        k.OpenMutexW.argtypes = (ctypes.c_uint, ctypes.c_int, ctypes.c_wchar_p)
        k.CloseHandle.restype = ctypes.c_int
        k.CloseHandle.argtypes = (ctypes.c_void_p,)
        k.CreateEventW.restype = ctypes.c_void_p
        k.CreateEventW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_wchar_p)
        k.OpenEventW.restype = ctypes.c_void_p
        k.OpenEventW.argtypes = (ctypes.c_uint, ctypes.c_int, ctypes.c_wchar_p)
        k.SetEvent.argtypes = (ctypes.c_void_p,)
        k.ResetEvent.argtypes = (ctypes.c_void_p,)
        k.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        k.WaitForSingleObject.restype = ctypes.c_uint
        _K32 = k
    return _K32


def acquire(name: str = "campusnet-watch") -> bool:
    """尝试成为唯一的 watch 实例；抢到返回 True，已被持有返回 False。"""
    global _HANDLE
    if _HANDLE is not None:
        return True  # 本进程已持有
    if os.name == "nt":
        import ctypes

        # use_last_error=True 是必须的：ctypes 自己的内部调用会把
        # GetLastError 冲掉，只有它保存的线程级错误码才可靠
        kernel32 = _kernel32()
        handle = kernel32.CreateMutexW(None, False, "Local\\" + name)
        if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
            # 抢不到就把刚拿到的句柄还回去！不还的话本进程会替那个互斥体
            # 一直撑着引用计数，对方退出之后这里仍然显示"锁被占用" ——
            # 界面会永远以为守护还在跑，并且再也不去拉起新的守护。
            if handle:
                kernel32.CloseHandle(handle)
            return False
        _HANDLE = handle
        return True
    import fcntl

    lock_path = os.path.join(
        os.environ.get("TMPDIR", "/tmp"), "." + name + ".lock"
    )
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    os.write(fd, str(os.getpid()).encode())
    _HANDLE = fd  # 故意不 close：进程活着，锁就在
    return True


def is_taken(name: str = "campusnet-watch") -> bool:
    """只查不占：这把锁是不是已经被**别的进程**拿着。

    比 ``acquire() + release()`` 干净 —— 那种写法每次查询都要真的去创建
    一次互斥体，稍有闪失就会把自己的句柄留在那，反把锁"撑"成永远占用。
    """
    if os.name != "nt":
        return _probe_posix(name)
    SYNCHRONIZE = 0x00100000
    kernel32 = _kernel32()
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, "Local\\" + name)
    if not handle:
        return False          # 打不开 = 对象不存在 = 没人拿着
    kernel32.CloseHandle(handle)
    return True


def _probe_posix(name: str) -> bool:
    import fcntl

    lock_path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "." + name + ".lock")
    if not os.path.exists(lock_path):
        return False
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return True
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
    return False


def hold_stop_event(name: str = STOP_NAME) -> bool:
    """守护侧：创建并持有"请退出"事件。

    有了它，界面点一下「取消开机自动连接」就能**立刻**把守护叫停 ——
    否则只能等它跑完当前那一轮（常规节奏下是 3 分钟）。
    """
    global _STOP_HANDLE
    if os.name != "nt":
        return False
    if _STOP_HANDLE is not None:
        return True
    handle = _kernel32().CreateEventW(None, True, False, "Local\\" + name)
    if not handle:
        return False
    _STOP_HANDLE = handle
    return True


def wait_stop(timeout: float) -> bool:
    """守护侧：最多等 ``timeout`` 秒。收到退出信号返回 True。

    没有事件（非 Windows / 创建失败）时退化成普通 ``sleep``，行为不变。
    """
    if _STOP_HANDLE is None:
        time.sleep(timeout)
        return False
    WAIT_OBJECT_0 = 0
    kernel32 = _kernel32()
    rc = kernel32.WaitForSingleObject(_STOP_HANDLE, int(max(0.0, timeout) * 1000))
    if rc != WAIT_OBJECT_0:
        return False
    kernel32.ResetEvent(_STOP_HANDLE)  # 复位，免得后面每次调用都"已触发"
    return True


def signal_stop(name: str = STOP_NAME) -> bool:
    """界面侧：通知正在跑的守护退出。没有守护在跑时返回 False。"""
    if os.name != "nt":
        return False
    EVENT_MODIFY_STATE = 0x0002
    kernel32 = _kernel32()
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, "Local\\" + name)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def release() -> None:
    """显式释放锁（一般用不到 —— 进程退出时内核自动清理）。"""
    global _HANDLE
    if _HANDLE is None:
        return
    if os.name == "nt":
        # 不 CloseHandle 的话，命名互斥体因为本进程还握着句柄而继续存在，
        # 别的进程会一直看到 ERROR_ALREADY_EXISTS
        if _HANDLE:
            _kernel32().CloseHandle(_HANDLE)
    elif isinstance(_HANDLE, int):
        os.close(_HANDLE)
    _HANDLE = None
