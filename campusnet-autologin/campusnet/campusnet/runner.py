"""编排：探测 → 选择认证方式 → 登录 → 校验。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import popup
from .config import Config
from .detector import Detection, NetStatus, check_online, detect, portal_candidates, provider_order
from .providers import LoginResult, get_provider
from .session import Session, local_ip, local_mac
from .wifi import WifiResult, ensure_wifi

LEVELS = ("debug", "info", "ok", "warn", "error")


def _null_log(message: str, level: str = "info") -> None:
    pass


def _usable_ip(ip: str) -> bool:
    """这个 IPv4 是不是真能用。

    开机刚关联上 Wi-Fi 时 ``local_ip()`` 会给出空串，或者一个
    ``169.254.x.x``（DHCP 没拿到地址时的自动私有地址）。拿它们去填
    ``wlanuserip`` 只会让门户回一句「认证超时」—— 所以都算"还没好"。
    """
    if not ip:
        return False
    if ip.startswith("169.254.") or ip.startswith("127."):
        return False
    return True


@dataclass
class RunResult:
    ok: bool = False
    skipped: bool = False
    provider: str = ""
    message: str = ""
    status_before: Optional[NetStatus] = None
    detection: Optional[Detection] = None
    attempts: List[LoginResult] = field(default_factory=list)


class Runner:
    """把配置、HTTP 会话、provider 串起来。"""

    def __init__(self, cfg: Config, logger: Optional[Callable[[str, str], None]] = None) -> None:
        self.cfg = cfg
        self.log = logger or _null_log
        self._session: Optional[Session] = None
        #: 等 Wi-Fi 关联的超时（秒）。命令行可覆盖。
        self.wifi_timeout: float = float(cfg.options.get("wifi_timeout", 30))
        #: 开机场景的 Wi-Fi 策略 —— 只在守护模式（watch）里用。
        #: 手动 login 不启用：要给"一次尝试 + 立刻给结论"的快速反馈。
        self.wifi_attempts: int = int(cfg.options.get("wifi_attempts", 2))
        self.wifi_retry_delay: float = float(cfg.options.get("wifi_retry_delay", 5))
        self.wifi_ready_timeout: float = float(cfg.options.get("wifi_ready_timeout", 12))
        #: 开机时等 DHCP 把 IP 发下来（见 :meth:`_client_ip`）的上限（秒）。
        self.client_ip_timeout: float = float(cfg.options.get("client_ip_timeout", 12))
        #: 可选的外部"请退出"信号：``stopper(秒) -> 是否该退出``。
        #: 守护循环里用它替代 ``time.sleep``，这样界面点一下「取消开机自启」
        #: 就能把守护立刻叫停，而不是让它傻等完当前这一轮（最长 3 分钟）。
        self.stopper: Optional[Callable[[float], bool]] = None
        #: 上一轮结束时是不是通的。用来抓「离线 → 在线」这个转变点 ——
        #: 只在那一刻收拾系统弹的登录页，不必每轮都白扫一遍。
        self._was_online: Optional[bool] = None
        #: 最近一次 :meth:`grab_online` 是不是被"退出"信号打断的。
        #: 用它把「用户主动取消」和「超时没连上」分开 —— 只有后者才该去问用户。
        self.interrupted: bool = False

    def _sleep(self, seconds: float) -> bool:
        """可被打断的等待。返回 True 表示收到了"退出"信号。"""
        if self.stopper is not None:
            try:
                return bool(self.stopper(seconds))
            except Exception:  # noqa: BLE001 - 信号出问题就退回普通等待
                pass
        time.sleep(seconds)
        return False

    # ------------------------------------------------------------ 资源
    @property
    def session(self) -> Session:
        if self._session is None:
            self._session = Session(
                timeout=self.cfg.timeout,
                use_proxy=self.cfg.use_proxy,
                logger=lambda msg: self.log(msg, "debug"),
            )
        return self._session

    @property
    def client_ip(self) -> str:
        return local_ip()

    def _client_ip(self, timeout: Optional[float] = None, quiet: bool = False) -> str:
        """等本机拿到校园网的 IPv4 再返回。

        这是"开机要等两分钟"的**头号原因**：刚关联上 Wi-Fi 的那几秒 DHCP
        还没走完，``local_ip()`` 给的是空串或 169.254.x.x，拿它去拼认证请求
        就变成 ``wlanuserip=``（空），门户只能回一句「认证超时」——
        整轮白费，然后等下一轮重试。

        ``local_ip()`` 只是对 UDP socket 做一次 connect（不发包，微秒级），
        所以这里密集轮询它几乎没有成本，却能把第一次就成功的概率拉满。
        """
        limit = self.client_ip_timeout if timeout is None else timeout
        deadline = time.time() + max(0.0, float(limit))
        ip = local_ip()
        while not _usable_ip(ip):
            if time.time() >= deadline:
                # ``quiet``：调用方只是"顺手等一下"，等不到不算问题
                # （真正要报"等不到 IP"的是认证前那次把关）
                if not quiet:
                    self.log("等了 {} 秒还没拿到校园网 IP（DHCP 未完成），仍按原样试一次".format(
                        int(limit)), "warn")
                return ip
            if self._sleep(0.2):
                return ip
            ip = local_ip()
        return ip

    # ------------------------------------------------------------ 探测
    def status(self) -> NetStatus:
        hint = self.cfg.portal_ip and (self.cfg.portal_ip if "//" in self.cfg.portal_ip
                                       else "http://" + self.cfg.portal_ip + "/")
        return check_online(self.session, hint or "")

    def detect(self, status: Optional[NetStatus] = None) -> Detection:
        detection = detect(self.session, self.cfg, status)
        if detection.scores:
            self.log("识别到认证系统：{}（置信度 {:.2f}）".format(
                detection.best, detection.scores[0][1]), "ok")
        else:
            self.log("未能识别门户类型，将按通用顺序尝试", "warn")
        return detection

    # ------------------------------------------------------------ Wi-Fi
    def ensure_wifi(self, patient: bool = False) -> WifiResult:
        """把 Wi-Fi 抢回校园网。

        **必须在联网探测之前做。** 反过来就会出现这个 bug：

        连着手机热点 → 热点能上网 → 探测说「已联网」→ 直接返回，Wi-Fi 永远不切。

        所以这里的判断依据是「当前连的是不是校园网」，而不是「有没有网」。
        只有配置了 ``wifi_ssid`` 才会动手；没配就完全不影响原有行为。

        ``patient=True`` 用"开机级"的耐心：先等网卡就绪，失败还重试几次。
        开机那十几秒里无线驱动和 ``WlanSvc`` 常没准备好，一次不成很正常 ——
        这正是"有时候开机 wifi 没有自动连上"的来源。手动 ``login`` 不传它，
        保持一次尝试、快速给结论。

        注意 ``patient=False`` 时**只传原本那三个参数**：模块级 ``ensure_wifi``
        的其余参数走默认值，行为与旧版逐字一致。
        """
        kwargs = {"timeout": self.wifi_timeout, "logger": self.log}
        if patient:
            kwargs.update(
                attempts=self.wifi_attempts,
                retry_delay=self.wifi_retry_delay,
                ready_timeout=self.wifi_ready_timeout,
            )
        result = ensure_wifi(self.cfg.wifi_ssid, **kwargs)

        if not result.supported:
            self.log(result.message, "debug")
            return result
        if not result.ok:
            # 没抢到就是后续认证失败的根因，必须说清楚，别让人去查账号密码
            self.log("没能连上校园 Wi-Fi「{}」—— 网络没切过去，认证一定不会成功".format(
                self.cfg.wifi_ssid), "warn")
            return result
        if result.changed:
            # 刚换了 Wi-Fi，DHCP 还没走完。这里**不盲等**：盯着 IP，一到就走
            # （最多 wifi_settle_delay 秒，默认 1.5）。原来固定 sleep 1 秒，
            # 而 DHCP 往往 0.3 秒就好了 —— 那一秒纯属白等。
            # 真正决定成败的"IP 到手没有"，仍由 :meth:`_client_ip` 在认证前把关。
            settle = float(self.cfg.options.get("wifi_settle_delay", 1.5))
            if settle > 0:
                self._client_ip(timeout=settle, quiet=True)
        return result

    # ------------------------------------------------------------ 主流程
    def ensure_online(self, force: bool = False, limit: int = 3,
                      patient: bool = False, wifi_checked: bool = False,
                      status: Optional[NetStatus] = None) -> RunResult:
        """确认网络可用，必要时完成认证。

        ``wifi_checked=True`` 表示调用方**已经**做过 Wi-Fi 那一环了，
        这里就不再重复发一遍 ``netsh``（守护模式每轮都先自己抢一次网）。

        ``status`` 是调用方**刚探到**的网络状态，传进来就直接复用：
        一次 ``check_online`` 要并发打三四个请求，守护每轮在探测上重复
        烧掉的时间比认证本身还多。
        """
        cfg = self.cfg

        # 先抢 Wi-Fi，再判联网 —— 顺序不能反，理由见 ensure_wifi 的注释。
        if not wifi_checked:
            self.ensure_wifi(patient=patient)

        if status is None:
            status = self.status()
        if status.online and not force:
            return RunResult(ok=True, skipped=True, message="已联网，无需认证", status_before=status)

        if status.online and force:
            self.log("当前已联网，但指定了 --force，仍要重新认证一次", "warn")

        if not cfg.username:
            return RunResult(ok=False, message="配置里没有账号，请先运行：campusnet setup", status_before=status)

        password = cfg.resolve_password(prompt=False)
        if not password:
            return RunResult(ok=False,
                             message="没有取到密码。请设置 CAMPUSNET_PASSWORD 环境变量，或用 campusnet setup 配置",
                             status_before=status)

        # 配置里写死了 provider（绝大多数情况）时，门户指纹识别这一步整个是白花的：
        # ``provider_order`` 会直接返回那一个名字，识别结果的唯一用途（排序）
        # 被彻底忽略，却要付出一次门户页 GET（未认证时它多半就是那串探测地址，
        # 等于再打一遍刚打过的请求）。
        fixed = bool(cfg.provider) and cfg.provider != "auto"
        detection = Detection() if fixed else self.detect(status)
        portal = detection.portal or (portal_candidates(cfg, status) or [""])[0]
        if not portal:
            return RunResult(ok=False, message="找不到认证门户地址，请用 --portal 指定或写入配置",
                             status_before=status, detection=detection)

        order = provider_order(cfg, detection, limit=limit)
        self.log("认证门户：{}".format(portal.rstrip("/")), "info")
        if fixed:
            self.log("按配置使用认证方式：{}".format(cfg.provider), "info")
        else:
            self.log("尝试顺序：{}".format(" → ".join(order)), "info")

        # 等 DHCP 把 IP 发下来再动手，别拿着空的 wlanuserip 去撞门户
        ip, mac = self._client_ip(), local_mac()
        attempts: List[LoginResult] = []

        for name in order:
            try:
                provider = get_provider(name)(self.session, cfg.options)
            except KeyError as exc:
                self.log(str(exc), "error")
                continue
            self.log("使用 {} 登录…".format(provider.display_name), "info")
            try:
                result = provider.login(portal, cfg.username, password, ip, mac)
            except Exception as exc:  # noqa: BLE001 - 单个 provider 崩了不该中断整体
                result = LoginResult(False, name, "内部异常：{}".format(exc))
            attempts.append(result)
            self.log(result.short(), "ok" if result.ok else "warn")

            if result.already_online:
                return RunResult(ok=True, provider=name, message=result.message,
                                 status_before=status, detection=detection, attempts=attempts)

            if not result.ok:
                if result.raw:
                    self.log(result.raw, "debug")
                continue

            # 接口说成功还不够，必须联网校验通过
            if force and status.online:
                return RunResult(ok=True, provider=name, message=result.message,
                                 status_before=status, detection=detection, attempts=attempts)

            if result.verified:
                # provider 提交完已经自己抓过真实网页验过了（跟我们这套判据
                # 完全一样），这里再等 verify_delay 秒 + 整轮探测一遍纯属重复。
                return RunResult(ok=True, provider=name, message="认证成功，网络已连通",
                                 status_before=status, detection=detection, attempts=attempts)

            wait = float(cfg.options.get("verify_delay", 2))
            if wait:
                time.sleep(wait)
            after = self.status()
            if after.online:
                return RunResult(ok=True, provider=name, message="认证成功，网络已连通",
                                 status_before=status, detection=detection, attempts=attempts)
            self.log("接口返回成功，但联网校验未通过，继续尝试其它方式", "warn")

        message = attempts[-1].message if attempts else "没有可用的认证方式"
        return RunResult(ok=False, message=message, status_before=status,
                         detection=detection, attempts=attempts)

    def watch(self, interval_minutes: int = 10, on_event=None,
              warmup_seconds: Optional[float] = None, warmup_gap: Optional[float] = None,
              retry_gap: Optional[float] = None):
        """常驻守护：联网正常时完全安静，断了就补登录。

        分两个阶段，这是为了解决「有时候开机 wifi 没有自动连上」：

        **阶段一 · 开机热身。** 守护进程是**登录时**启动的，而这段时间
        无线驱动、``WlanSvc``、SSID 广播往往还没准备好。旧逻辑一轮不成
        就要等满 ``interval``（默认 10 分钟），用户体感就是"开机一直没网"。
        所以开头 ``warmup_seconds`` 秒内改成每 ``warmup_gap`` 秒重试一次，
        一旦联网立刻进入常规节奏。

        **阶段二 · 常规守护。** 联网正常就按 ``interval`` 检查；
        哪一轮没弄通就用 ``retry_gap`` 快速再试，而不是干等十分钟 ——
        合盖唤醒、中途被切到热点、会话超时都能很快补回来。
        """
        if warmup_seconds is None:
            warmup_seconds = float(self.cfg.options.get("wifi_warmup", 180))
        if warmup_gap is None:
            warmup_gap = float(self.cfg.options.get("wifi_warmup_gap", 5))
        if retry_gap is None:
            retry_gap = float(self.cfg.options.get("wifi_retry_gap", 60))

        self.log("守护模式启动，每 {} 分钟检查一次".format(interval_minutes), "ok")
        if self.cfg.wifi_ssid:
            self.log("已配置校园 Wi-Fi「{}」，每轮都会确认是否连在它上面".format(self.cfg.wifi_ssid), "info")

        try:
            if warmup_seconds > 0 and self._warmup(warmup_seconds, warmup_gap, on_event):
                self.log("收到退出信号，守护结束", "info")
                return
            while True:
                online = self._watch_round_safe(on_event)
                gap = max(30, interval_minutes * 60) if online else max(15.0, retry_gap)
                if self._sleep(gap):
                    self.log("收到退出信号，守护结束", "info")
                    return
        except KeyboardInterrupt:
            self.log("收到中断，退出守护模式", "info")
            return

    def grab_online(self, seconds: float = 180.0, gap: float = 5.0,
                    on_event=None) -> bool:
        """开机抢网：在 ``seconds`` 秒内反复尝试，**连上就收工**。

        和 :meth:`watch` 的分工很清楚：``watch`` 是常驻守护 —— 连上之后还要
        每 ``interval`` 分钟查一轮、掉线补登录；这里只管"把网弄通"这一件事，
        连上就返回 ``True``，之后掉不掉线一概不管（掉线由调用方决定怎么处理）。

        超时仍未连上返回 ``False``，让调用方决定是收工还是再问用户一次。

        中途收到"退出"信号（界面里点了取消）会提前结束，此时
        :attr:`interrupted` 为 ``True``，调用方据此区分「被叫停」和「没连上」。
        """
        self._was_online = False
        # ``_warmup`` 的返回值就是"收到退出信号了吗"
        self.interrupted = self._warmup(seconds, gap, on_event)
        return bool(self._was_online)

    def _warmup(self, seconds: float, gap: float, on_event) -> bool:
        """开机后的集中抢网阶段：一直试到联网，或超过 ``seconds``。

        返回 True 表示中途收到了"退出"信号（调用方据此收摊）。

        节奏是**立刻试第一次，之后每 ``gap`` 秒一次** —— 不是"先等 gap 再试"。
        守护是登录时被拉起来的，那一刻网卡 / ``WlanSvc`` / SSID 往往还没就绪，
        所以第一次多半不成功；但它只是本地探测，零成本，而万一是休眠唤醒、
        手动点了一次这类"已经具备条件"的场景，就能马上收工、少等一个 gap。
        """
        self.log("开机热身：{} 秒内每 {} 秒试一次（立即开始），直到联网".format(
            int(seconds), int(gap)), "info")
        deadline = time.time() + seconds
        round_no = 0
        while True:
            round_no += 1
            if round_no > 1:
                self.log("热身第 {} 轮重试…".format(round_no), "info")
            if self._watch_round_safe(on_event):
                self.log("热身阶段已联网", "ok")
                return False
            remaining = deadline - time.time()
            if remaining <= 0:
                self.log("热身结束仍未联网，转为常规检查（这样也不会一直空转）", "warn")
                return False
            if self._sleep(max(1.0, min(gap, remaining))):
                return True

    def _watch_round_safe(self, on_event) -> bool:
        """ ``_watch_round`` 的保险版：**单轮出错绝不能让守护进程退出**。

        守护进程要活好几天，中途 provider 抛异常、系统命令抽风都是常态，
        所以每一轮单独兜住异常，出错就当"这轮没成功"，下一轮接着来。
        """
        try:
            return self._watch_round(on_event)
        except Exception as exc:  # noqa: BLE001 - 守护进程必须活到最后
            self.log("本轮检查出错：{}".format(exc), "error")
            return False

    def _watch_round(self, on_event) -> bool:
        """跑一轮：确认 Wi-Fi → 判断联网 → 必要时登录。返回是否已联网。

        Wi-Fi 这一环自己先做掉，然后告诉 ``ensure_online`` 别再重复一次 ——
        开机场景下每次重试都可能等网卡、等关联，省一次是一次。
        探到的 ``status`` 也一并传进去，省掉一次完整的联网探测。

        顺带把这一轮花了多久写进日志：开机"卡在哪一步"全靠它。
        """
        started = time.time()
        try:
            return self._watch_round_inner(on_event)
        finally:
            self.log("本轮耗时 {:.1f} 秒".format(time.time() - started), "info")

    def _watch_round_inner(self, on_event) -> bool:
        wifi = self.ensure_wifi(patient=True)
        if wifi.changed:
            # 刚刚才切到这个 Wi-Fi：先把 DHCP 等出来再探测。不然这一轮的
            # 联网探测必然白打（没有任何出口），只是白白烧掉几秒超时。
            self._client_ip()
        status = self.status()
        if status.online:
            if wifi.changed:
                self.log("已切回校园 Wi-Fi，网络正常", "ok")
            self._note_online()
            return True

        self.log("检测到网络未认证，开始自动登录…", "warn")
        result = self.ensure_online(patient=True, wifi_checked=True, status=status)
        if on_event:
            on_event(result)
        self.log(result.message, "ok" if result.ok else "error")
        if result.ok:
            self._note_online()
        else:
            # 这轮没弄通：记住"现在是离线"，这样下次一恢复就能再抓一次转变点
            self._was_online = False
        return result.ok

    def _note_online(self) -> None:
        """记下"此刻网络是通的"，并在刚接通的那一刻收拾系统弹的登录页。

        Windows 在检测到"已连上但没认证"的网络时，会**自己**打开默认浏览器
        到门户登录页（微软 KB 4494446，by design）。我们既然已经把网自动登
        好了，那张页面就是多余的，顺手关掉 —— 细节和判据见 ``popup`` 模块。

        只在「离线 → 在线」的转变点动手：一来够用（弹窗只发生在刚连上时），
        二来省得每轮都白扫一遍。
        """
        first_time = self._was_online is not True
        self._was_online = True
        if not first_time:
            return
        if not popup.enabled(self.cfg.options):
            return
        popup.sweep_async(log=self.log)
