# coding:utf-8
"""
iOS 性能测试模块
go-ios 负责设备发现/tunnel/截图/电池，
py-ios-device 通过 Instruments DTX 协议采集 CPU/内存/网络/磁盘。

go-ios 需要先启动 tunnel (iOS 17+):
    ENABLE_GO_IOS_AGENT=user /path/to/ios tunnel start --userspace

可用命令:
    ios list                    设备列表
    ios info                    设备信息
    ios ps                      进程列表 (需要 tunnel)
    ios apps                    应用列表
    ios screenshot              截图
    ios batterycheck/batteryregistry  电池
    ios diskspace               磁盘空间
"""
import asyncio
import dataclasses
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import threading
from pathlib import Path
from typing import Optional, Dict, List

from client_perf.log import log as logger
from client_perf.core.monitor import Monitor

# ── py-ios-device (Instruments DTX 协议) ──────────────────────────────────
# 可选依赖：缺失时整体降级，go-ios 基础能力（列表/截图/电池）仍可用，
# 仅 Instruments 高级采集（CPU/内存/网络/磁盘/FPS/GPU）不可用。
try:
    from ios_device.remote.remote_lockdown import RemoteLockdownClient
    from ios_device.util.lockdown import LockdownClient
    from ios_device.cli.base import InstrumentsBase
    _PY_IOS_DEVICE_AVAILABLE = True
except Exception as _ios_device_import_err:  # 常见：未安装 py-ios-device / pyOpenSSL 版本不兼容
    logger.warning(
        f"py-ios-device 不可用，Instruments 高级采集降级: {_ios_device_import_err}"
    )
    RemoteLockdownClient = None
    LockdownClient = None
    InstrumentsBase = None
    _PY_IOS_DEVICE_AVAILABLE = False

# ─────────────────────────── go-ios 路径 ───────────────────────────

# 按优先级查找 go-ios
# Downloads 里的版本优先（已验证支持 userspace tunnel）
# 环境变量 GO_IOS_PATH 可覆盖
# 从 tool/go-ios-bin 目录中选择合适的 ios 工具
def get_ios_tool_path():
    """根据当前平台返回合适的 ios 工具路径"""
    tool_dir = Path(__file__).parent.parent.joinpath("tool", "go-ios-bin")
    
    if sys.platform == "win32":
        return tool_dir.joinpath("go-ios-win", "ios.exe")
    elif sys.platform == "darwin":
        return tool_dir.joinpath("go-ios-mac", "ios")
    elif sys.platform == "linux":
        # 根据架构选择
        if platform.machine() == "arm64":
            return tool_dir.joinpath("go-ios-linux", "ios-arm64")
        else:
            return tool_dir.joinpath("go-ios-linux", "ios-amd64")
    return None

_bundled_ios_path = get_ios_tool_path()
_BUNDLED_IOS = str(_bundled_ios_path) if _bundled_ios_path else None
GO_IOS_PATH = (
    os.environ.get("GO_IOS_PATH")
    or (_BUNDLED_IOS if _BUNDLED_IOS and os.path.isfile(_BUNDLED_IOS) else None)
    or shutil.which("ios")
    or shutil.which("go-ios")
)

if not GO_IOS_PATH or not os.path.isfile(GO_IOS_PATH):
    GO_IOS_PATH = None
    logger.warning("go-ios 未找到，iOS 性能测试不可用")
elif os.name != "nt" and not os.access(GO_IOS_PATH, os.X_OK):
    logger.warning("go-ios 不可执行，请检查文件权限: %s", GO_IOS_PATH)


def _go_ios_env() -> dict:
    """返回执行 go-ios 命令所需的环境变量（含 ENABLE_GO_IOS_AGENT=user 以连接 userspace tunnel）"""
    env = os.environ.copy()
    env.setdefault("ENABLE_GO_IOS_AGENT", "user")
    return env


def _run(args: list, timeout: int = 15) -> Optional[str]:
    """执行 go-ios 命令，返回有内容的输出（优先 stdout，其次 stderr）"""
    if not GO_IOS_PATH:
        return None
    try:
        result = subprocess.run(
            [GO_IOS_PATH] + args,
            capture_output=True, text=True, timeout=timeout,
            env=_go_ios_env(),
            encoding="utf-8"
        )
        # go-ios 有些命令数据走 stdout，有些走 stderr（logrus 格式）
        out = result.stdout.strip()
        if out:
            return out
        err = result.stderr.strip()
        if err:
            return err
        return None
    except subprocess.TimeoutExpired:
        logger.error(f"go-ios {' '.join(args)} 超时")
    except Exception as e:
        logger.error(f"go-ios {' '.join(args)} 异常: {e}")
    return None


def _run_json(args: list, timeout: int = 15):
    """执行 go-ios 命令，返回解析后的 JSON 或 None"""
    raw = _run(args, timeout)
    if not raw:
        return None
    # go-ios 既可能输出单个/格式化多行 JSON，也可能在 JSON 前输出日志行。
    # 先尝试整体解析，兼容 `tunnel ls` 的多行数组；失败后再从每个可能的
    # JSON 起始位置解析，最后兼容逐行 JSON 日志格式。
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if not text[index + end:].strip():
            return value

    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


# ─────────────────────────── Tunnel 管理 ───────────────────────────

def _is_admin() -> bool:
    if platform.system() != "Windows":
        return True
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


class TunnelManager:
    """管理 go-ios tunnel"""
    _proc: Optional[subprocess.Popen] = None
    _tunnel_procs: List[subprocess.Popen] = []
    _admin_warned = False
    # 保护 ensure_tunnel/start，避免并发重复启动同一个 tunnel
    _lock = threading.Lock()

    @classmethod
    def ensure_tunnel(cls, udid: str = "") -> bool:
        """确保指定设备的 tunnel 已启动，返回是否可用。

        加锁 + 按 UDID 判断现有 tunnel：若 tunnel ls 已包含该设备的
        可用连接，则直接返回，避免重复启动。
        """
        with cls._lock:
            if cls.tunnel_info_for_udid(udid):
                return True
            return cls.start(udid)

    @classmethod
    def tunnel_info(cls) -> Optional[list]:
        """查询已有 tunnel 列表"""
        data = _run_json(["tunnel", "ls"])
        if isinstance(data, list) and data:
            return data
        return None

    @classmethod
    def tunnel_info_for_udid(cls, udid: str = "") -> Optional[Dict]:
        """从已有 tunnel 中筛选指定设备的连接；未指定 UDID 时返回第一条。"""
        tunnels = cls.tunnel_info()
        if not tunnels:
            return None
        return _select_tunnel(tunnels, udid)

    @classmethod
    def start(cls, udid: str = "") -> bool:
        """启动 tunnel（后台进程）

        iOS 17+ 设备通过 DTX 协议采集数据需要先启动 go-ios tunnel，
        在 Windows 上此操作需要管理员权限。
        """
        if not GO_IOS_PATH:
            return False

        if platform.system() == "Windows" and not _is_admin():
            if not cls._admin_warned:
                cls._admin_warned = True
                logger.error(
                    "iOS 17+ 设备的 tunnel 启动需要管理员权限。"
                    "请以管理员身份运行 client-perf，或启动时不要使用 --no-elevate 参数。"
                )
            return False

        args = [GO_IOS_PATH, "tunnel", "start", "--userspace"]
        if udid:
            args += ["--udid", udid]
        env = os.environ.copy()
        env["ENABLE_GO_IOS_AGENT"] = "user"
        # 注意：不读取 stdout/stderr，因此使用 DEVNULL 而非 PIPE，
        # 避免子进程阻塞在填满管道缓冲区（未消费的 PIPE 导致死锁）。
        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=env
            )
            cls._proc = proc
            cls._tunnel_procs.append(proc)
            # 等待 tunnel 建立
            for _ in range(20):
                time.sleep(1)
                if cls.tunnel_info_for_udid(udid):
                    logger.info("go-ios tunnel 已启动")
                    return True
            logger.error("go-ios tunnel 启动超时")
        except Exception as e:
            logger.error(f"go-ios tunnel 启动失败: {e}")
        return False

    @classmethod
    def stop(cls):
        if cls._proc and cls._proc.poll() is None:
            cls._proc.terminate()
            cls._proc = None
        # 也尝试 stopagent
        _run(["tunnel", "stopagent"], timeout=5)

    @classmethod
    def stop_all_tunnels(cls):
        """停止所有 tunnel 进程"""
        for proc in cls._tunnel_procs:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass
        cls._tunnel_procs.clear()
        cls._proc = None
        _run(["tunnel", "stopagent"], timeout=5)


# ─────────────────────────── 设备发现 ───────────────────────────

def get_ios_devices() -> List[Dict]:
    """获取已连接的 iOS 设备列表"""
    data = _run_json(["list"])
    if not data:
        return []
    udid_list = data.get("deviceList", []) if isinstance(data, dict) else []
    devices = []
    for udid in udid_list:
        info = _run_json(["info", "--udid", udid]) or {}
        devices.append({
            "udid": udid,
            "model": info.get("DeviceName", "Unknown"),
            "product_type": info.get("ProductType", "Unknown"),
            "ios_version": info.get("ProductVersion", "Unknown"),
            "status": "online",
            "device_type": "ios",
            # 统一字段，与 Android 保持一致
            "serial": udid,
            "name": info.get("DeviceName", "Unknown"),
        })
    return devices


def _first_udid() -> Optional[str]:
    """获取第一个设备的 UDID"""
    data = _run_json(["list"])
    if data:
        dl = data.get("deviceList", []) if isinstance(data, dict) else []
        if dl:
            return dl[0]
    return None


def _parse_ios_major(version: object) -> Optional[int]:
    """解析 iOS 主版本号；无法识别时返回 None。"""
    if version is None:
        return None
    try:
        return int(str(version).strip().split(".", 1)[0])
    except (TypeError, ValueError):
        return None


# ── iOS 版本缓存 ──────────────────────────────────────────────
# 避免每个 sysmontap / graphics 连接都重复执行 `ios info`。
# 仅在查询成功（拿到非 None 版本）时缓存，查询失败不缓存以便后续重试。
_IOS_VERSION_CACHE: Dict[str, str] = {}
_IOS_VERSION_CACHE_LOCK = threading.Lock()


def _get_ios_version(udid: str) -> Optional[str]:
    """通过 go-ios 查询指定设备的系统版本（带进程内缓存）。"""
    with _IOS_VERSION_CACHE_LOCK:
        cached = _IOS_VERSION_CACHE.get(udid)
        if cached is not None:
            return cached
    info = _run_json(["info", "--udid", udid]) or {}
    version = info.get("ProductVersion") if isinstance(info, dict) else None
    version = str(version).strip() if version else None
    if version:
        with _IOS_VERSION_CACHE_LOCK:
            _IOS_VERSION_CACHE[udid] = version
    return version


def _clear_ios_version_cache(udid: Optional[str] = None) -> None:
    """清除 iOS 版本缓存（设备断开或版本变化时调用）。"""
    with _IOS_VERSION_CACHE_LOCK:
        if udid is None:
            _IOS_VERSION_CACHE.clear()
        else:
            _IOS_VERSION_CACHE.pop(udid, None)


def _requires_tunnel(udid: str) -> bool:
    """iOS 17+ 使用 Remote Service Discovery；旧系统走 USB lockdown。"""
    version = _get_ios_version(udid)
    major = _parse_ios_major(version)
    if major is None:
        # 未知版本时不贸然启动 tunnel；先走 USB 直连，失败日志会保留原因。
        logger.warning(f"无法识别 iOS 版本，优先尝试 USB 直连: udid={udid}")
        return False
    return major >= 17


# ─────────────────────────── 自动获取前台应用 ───────────────────────────

def _get_foreground_app(udid: str) -> Optional[Dict]:
    """
    获取前台运行的应用信息 (pid + bundleId)
    通过 ps 列表中 IsApplication=true 且最近启动的应用判断
    """
    raw = _run(["ps", "--udid", udid])
    if not raw:
        return None
    # ps 输出是一个 JSON 数组
    lines = raw.strip().split("\n")
    for line in reversed(lines):
        try:
            processes = json.loads(line)
            if isinstance(processes, list):
                # 筛选 IsApplication=true 的进程，按 StartDate 倒序
                apps = [p for p in processes if p.get("IsApplication")]
                if apps:
                    apps.sort(key=lambda x: x.get("StartDate", ""), reverse=True)
                    return {
                        "pid": apps[0].get("Pid", 0),
                        "name": apps[0].get("Name", ""),
                        "bundle_id": _pid_to_bundle(udid, apps[0].get("Name", ""))
                    }
        except json.JSONDecodeError:
            continue
    return None


def _pid_to_bundle(udid: str, process_name: str) -> str:
    """通过进程名反查 bundle_id"""
    raw = _run(["apps", "--udid", udid])
    if not raw:
        return process_name
    lines = raw.strip().split("\n")
    for line in reversed(lines):
        try:
            apps = json.loads(line)
            if isinstance(apps, list):
                for app in apps:
                    exe = app.get("CFBundleExecutable", "")
                    if exe == process_name:
                        return app.get("CFBundleIdentifier", process_name)
        except json.JSONDecodeError:
            continue
    return process_name


def _find_pid_by_bundle(udid: str, bundle_id: str) -> int:
    """通过 bundle_id 查找 pid"""
    # 先查 executable name
    exe_name = bundle_id  # fallback
    raw_apps = _run(["apps", "--udid", udid])
    if raw_apps:
        for line in reversed(raw_apps.strip().split("\n")):
            try:
                apps = json.loads(line)
                if isinstance(apps, list):
                    for app in apps:
                        if app.get("CFBundleIdentifier") == bundle_id:
                            exe_name = app.get("CFBundleExecutable", bundle_id)
                            break
            except json.JSONDecodeError:
                continue

    raw_ps = _run(["ps", "--udid", udid])
    if raw_ps:
        for line in reversed(raw_ps.strip().split("\n")):
            try:
                processes = json.loads(line)
                if isinstance(processes, list):
                    for p in processes:
                        if p.get("Name") == exe_name:
                            return p.get("Pid", 0)
            except json.JSONDecodeError:
                continue
    return 0


# ─────────────────────────── 应用列表 ───────────────────────────

async def ios_apps(udid: str) -> List[Dict]:
    """获取已安装应用列表（异步，go-ios apps 可能较慢，超时 30 秒）"""
    def real_func():
        raw = _run(["apps", "--udid", udid], timeout=30)
        if not raw:
            return []
        lines = raw.strip().split("\n")
        for line in reversed(lines):
            try:
                apps = json.loads(line)
                if isinstance(apps, list):
                    result = []
                    for app in apps:
                        bid = app.get("CFBundleIdentifier", "")
                        if bid and app.get("ApplicationType") == "User":
                            result.append({
                                "bundle_id": bid,
                                "name": app.get("CFBundleDisplayName") or app.get("CFBundleName", bid),
                                "version": app.get("CFBundleShortVersionString", ""),
                                # 兼容 Android 字段
                                "package_name": bid,
                                "running": False,
                            })
                    return result
            except json.JSONDecodeError:
                continue
        return []

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=35)


# ─────────────────────────── 系统信息 ───────────────────────────

async def ios_sys_info(udid: str) -> Dict:
    """获取设备系统信息"""
    def real_func():
        info = _run_json(["info", "--udid", udid]) or {}
        disk = _run_json(["diskspace", "--udid", udid]) or {}
        return {
            "platform": "iOS",
            "computer_name": info.get("DeviceName", "Unknown"),
            "time": time.time(),
            "cpu_count": 0,
            "cpu_cores": 0,
            "cpu_name": info.get("CPUArchitecture", "Unknown"),
            "memory_total": 0,
            "ram": "Unknown",
            "rom": f"{round(disk.get('TotalBytes', 0) / (1024 ** 3), 1)}G",
            "disk_total": round(disk.get("TotalBytes", 0) / (1024 ** 3), 1),
            "product_type": info.get("ProductType", "Unknown"),
            "ios_version": info.get("ProductVersion", "Unknown"),
            "serial": udid,
        }
    return await asyncio.to_thread(real_func)


# ─────────────────────────── sysmontap 采集（CPU + 内存） ───────────────────────────

def _read_sysmontap_sample(udid: str, skip_count: int = 2) -> Optional[Dict]:
    """
    启动 sysmontap，读取第 skip_count 条有效数据后终止。
    sysmontap 输出字段（go-ios v1.0.x）:
      cpu_count, cpu_total_load, enabled_cpus,
      mem_free, mem_used, mem_total,
      net_bytes_in, net_bytes_out,
      disk_bytes_read, disk_bytes_written, ...
    返回原始 dict 或 None。
    """
    if not GO_IOS_PATH:
        return None
    try:
        proc = subprocess.Popen(
            [GO_IOS_PATH, "sysmontap", "--udid", udid],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=_go_ios_env(), encoding="utf-8"
        )
        count = 0
        result_data = None
        try:
            for line in iter(proc.stderr.readline, ''):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # 只要包含 cpu_total_load 就认为是有效数据
                if "cpu_total_load" in data:
                    count += 1
                    if count >= skip_count:
                        result_data = data
                        break
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        return result_data
    except Exception as e:
        logger.error(f"iOS sysmontap 采集失败: {e}")
        return None


# ─────────────────────────── Instruments Session (py-ios-device) ───────────────────────────

def _select_tunnel(tunnels: object, udid: str = "") -> Optional[Dict]:
    """从 tunnel 列表中选择指定设备；未指定 UDID 时返回第一条。"""
    if not isinstance(tunnels, list) or not tunnels:
        return None
    if not udid:
        return tunnels[0]
    for tunnel in tunnels:
        if not isinstance(tunnel, dict):
            continue
        tunnel_udid = tunnel.get("udid") or tunnel.get("serial") or tunnel.get("identifier")
        if tunnel_udid == udid:
            return tunnel
    return None


def _get_tunnel_info(udid: str = "") -> Optional[Dict]:
    """从 go-ios tunnel ls 获取指定设备 tunnel 的地址和端口。"""
    raw = _run(["tunnel", "ls"], timeout=5)
    if not raw:
        return None
    # 先尝试整体解析（tunnel ls 输出格式化多行 JSON）
    try:
        tunnel = _select_tunnel(json.loads(raw.strip()), udid)
        if tunnel:
            return tunnel
    except Exception:
        pass
    # fallback：逐行找 JSON 数组行（单行输出格式）
    for line in reversed(raw.strip().split("\n")):
        line = line.strip()
        if line.startswith("["):
            try:
                tunnel = _select_tunnel(json.loads(line), udid)
                if tunnel:
                    return tunnel
            except Exception:
                pass
    return None


class _InstrumentsSession:
    """
    通过 py-ios-device + go-ios tunnel 连接 Apple Instruments DTX 服务。

    【优化】后台持续采集模式：
      - start(pid) 启动后台线程，持续以 1s 间隔采集所有进程+系统数据
      - 各采集函数直接读缓存，响应时间 < 50ms
      - 连接断开后自动重连（最多 3 次）
      - stop() 停止后台线程并断开连接

    使用方式：
        session = _InstrumentsSession.get(udid)
        session.start(pid=66344)          # 首次调用时启动后台采集
        data = session.get_latest(pid)    # 立即返回最新缓存
    """

    # 进程级属性（有序列表，顺序决定 zip 映射）
    PROC_ATTRS = [
        'pid', 'name', 'cpuUsage',
        'physFootprint',        # 进程物理内存 bytes
        'diskBytesRead', 'diskBytesWritten',
        'threadCount',
    ]
    # 系统级属性
    SYS_ATTRS = [
        'vmUsedCount', 'vmFreeCount', 'physMemSize',
        'netBytesIn', 'netBytesOut',
        'diskBytesRead', 'diskBytesWritten',
    ]

    # 全局实例池（每个 udid 一个）
    _pool: Dict[str, "_InstrumentsSession"] = {}
    _pool_lock = threading.Lock()

    @classmethod
    def get(cls, udid: str) -> "_InstrumentsSession":
        with cls._pool_lock:
            if udid not in cls._pool:
                cls._pool[udid] = cls(udid)
            return cls._pool[udid]

    def __init__(self, udid: str):
        self.udid = udid
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()   # 保证 start() 只执行一次
        self._stop_event = threading.Event()
        self._data_ready = threading.Event()  # 首次数据就绪信号
        self._bg_thread: Optional[threading.Thread] = None
        self._running = False

        # 缓存：{pid: {proc_data}, "sys": {sys_data}, "ts": float}
        self._cache: Dict = {"sys": {}, "procs": {}, "ts": 0.0}
        # graphics 缓存：{"fps": int, "gpu": float, "gpu_renderer": float, "gpu_tiler": float, "ts": float}
        self._graphics_cache: Dict = {"fps": 0, "gpu": 0.0, "gpu_renderer": 0.0, "gpu_tiler": 0.0, "ts": 0.0}
        self._graphics_ready = threading.Event()
        self._graphics_thread: Optional[threading.Thread] = None
        self._graphics_start_lock = threading.Lock()
        self._graphics_running = False

        # 已订阅的 pid 集合
        self._watched_pids: set = set()

        # 动态属性列表（首次连接后填充）
        self._proc_attrs: List[str] = []
        self._sys_attrs:  List[str] = []

    # ── 连接管理 ──────────────────────────────────────────────────

    def _make_lockdown(self):
        if not _PY_IOS_DEVICE_AVAILABLE:
            raise RuntimeError(
                "py-ios-device 不可用，无法建立 Instruments 连接。"
                "请安装依赖: uv add py-ios-device"
            )

        version = _get_ios_version(self.udid)
        major = _parse_ios_major(version)

        if major is not None and major < 17:
            logger.info(
                f"[Instruments] iOS {version} 使用 USB lockdown 直连，"
                "无需 go-ios tunnel"
            )
            return LockdownClient(udid=self.udid, network=False)

        if major is None:
            logger.warning(
                f"[Instruments] 无法识别 iOS 版本，先尝试 USB lockdown 直连: "
                f"udid={self.udid}"
            )
            try:
                return LockdownClient(udid=self.udid, network=False)
            except Exception as usb_error:
                logger.warning(f"[Instruments] USB 直连失败，尝试 tunnel: {usb_error}")

        if not TunnelManager.ensure_tunnel(self.udid):
            raise RuntimeError(
                f"iOS {version or '17+'} 需要 go-ios tunnel，但自动启动失败。"
                "请检查设备连接、配对和权限。"
            )

        info = _get_tunnel_info(self.udid)
        if not info:
            raise RuntimeError("go-ios tunnel 已启动但未返回当前设备的可用连接信息")

        # 字段完整性校验，缺失时给出明确错误而非 KeyError
        address = info.get("address")
        rsd_port = info.get("rsdPort")
        if address is None or rsd_port is None:
            raise RuntimeError(
                "go-ios tunnel 连接信息不完整，缺少 address 或 rsdPort 字段: "
                f"{info}"
            )

        lockdown = RemoteLockdownClient(
            address=(address, rsd_port),
            userspace_port=info.get("userspaceTunPort")
        )
        lockdown.connect()
        return lockdown

    # ── 后台采集线程 ──────────────────────────────────────────────

    def _bg_loop(self):
        """后台线程：持续采集，断线自动重连"""
        retry = 0
        max_retry = 5
        while not self._stop_event.is_set() and retry < max_retry:
            try:
                self._run_sysmontap_loop()
                retry = 0  # 正常退出（stop_event 触发）则重置重试计数
            except Exception as e:
                if self._stop_event.is_set():
                    break
                retry += 1
                logger.warning(f"[Instruments] 连接断开，{retry}/{max_retry} 次重连: {e}")
                time.sleep(min(2 ** retry, 10))
        with self._lock:
            self._running = False
        logger.info(f"[Instruments] 后台采集线程退出 udid={self.udid}")

    def _run_sysmontap_loop(self):
        """建立一次连接并持续采集，直到 stop_event 触发或连接断开。

        【关键】只用 InstrumentsBase 一个对象管理连接：
          - base.device_info  → 查询设备支持的属性（内部懒建 instruments_rcp）
          - base.sysmontap()  → 持续采集（复用同一个 instruments_rcp）
          不要额外创建 InstrumentServer / InstrumentDeviceInfo，否则会抢占
          lockdown 连接导致 socket 3s 后断开。
        """
        lockdown = self._make_lockdown()
        base = InstrumentsBase(lockdown=lockdown)

        # 查询设备支持的属性（取交集）
        available_proc = base.device_info.sysmonProcessAttributes()
        available_sys  = base.device_info.sysmonSystemAttributes()
        proc_attrs = [a for a in self.PROC_ATTRS if a in available_proc]
        sys_attrs  = [a for a in self.SYS_ATTRS  if a in available_sys]

        base.process_attributes = proc_attrs
        base.system_attributes  = sys_attrs

        with self._lock:
            self._proc_attrs = proc_attrs
            self._sys_attrs  = sys_attrs

        logger.info(f"[Instruments] proc_attrs={proc_attrs}")
        logger.info(f"[Instruments] sys_attrs={sys_attrs}")

        def callback(res):
            sel = res.selector
            # 过滤握手/配置包（dict 格式，如 {'k':0,'tv':65536}）
            if not isinstance(sel, list):
                return
            ts = time.time()
            new_procs = {}
            new_sys   = {}
            for row in sel:
                if not isinstance(row, dict):
                    continue
                # 系统数据
                if "System" in row:
                    raw = row["System"]
                    if isinstance(raw, (list, tuple)) and len(raw) == len(sys_attrs):
                        new_sys = dict(zip(sys_attrs, raw))
                    elif isinstance(raw, dict):
                        new_sys = {k: raw.get(k) for k in sys_attrs}
                # 进程数据（所有进程都缓存，按 pid 索引）
                if "Processes" in row:
                    for pid_key, vals in row["Processes"].items():
                        try:
                            p = int(pid_key)
                        except (ValueError, TypeError):
                            p = pid_key
                        if isinstance(vals, (list, tuple)) and len(vals) == len(proc_attrs):
                            new_procs[p] = dict(zip(proc_attrs, vals))
                        elif isinstance(vals, dict):
                            new_procs[p] = {k: vals.get(k) for k in proc_attrs}
            with self._lock:
                if new_sys:
                    self._cache["sys"] = new_sys
                if new_procs:
                    self._cache["procs"].update(new_procs)
                if new_sys or new_procs:
                    self._cache["ts"] = ts
                    self._data_ready.set()   # 通知等待方：首次数据已就绪

        logger.info(f"[Instruments] 开始持续采集 udid={self.udid}")
        base.sysmontap(callback=callback, time=1000, stopSignal=self._stop_event)

        try:
            base.instruments.stop()
        except Exception:
            pass

    # ── Graphics 后台采集（FPS + GPU）────────────────────────────

    def _run_graphics_loop(self):
        """建立独立连接持续采集 FPS / GPU，直到 stop_event 触发。"""
        lockdown = self._make_lockdown()
        base = InstrumentsBase(lockdown=lockdown)

        def callback(res):
            sel = res.selector
            if not isinstance(sel, dict):
                return
            fps  = sel.get("CoreAnimationFramesPerSecond", 0) or 0
            gpu  = sel.get("Device Utilization %", 0.0) or 0.0
            rend = sel.get("Renderer Utilization %", 0.0) or 0.0
            tile = sel.get("Tiler Utilization %", 0.0) or 0.0
            with self._lock:
                self._graphics_cache = {
                    "fps": int(fps),
                    "gpu": round(float(gpu), 2),
                    "gpu_renderer": round(float(rend), 2),
                    "gpu_tiler": round(float(tile), 2),
                    "ts": time.time(),
                }
                self._graphics_ready.set()

        logger.info(f"[Instruments] 开始采集 FPS/GPU udid={self.udid}")
        base.graphics(callback=callback, time=1000, stopSignal=self._stop_event)
        try:
            base.instruments.stop()
        except Exception:
            pass

    def _graphics_bg_loop(self):
        """FPS/GPU 后台线程，断线自动重连"""
        retry = 0
        max_retry = 5
        while not self._stop_event.is_set() and retry < max_retry:
            try:
                self._run_graphics_loop()
                retry = 0
            except Exception as e:
                if self._stop_event.is_set():
                    break
                retry += 1
                logger.warning(f"[Instruments/FPS] 连接断开，{retry}/{max_retry} 次重连: {e}")
                time.sleep(min(2 ** retry, 10))
        with self._lock:
            self._graphics_running = False
        logger.info(f"[Instruments/FPS] 后台线程退出 udid={self.udid}")

    def start_graphics(self):
        """启动 FPS/GPU 后台采集线程（幂等）。阻塞直到首次数据就绪（最多 5s）。"""
        with self._graphics_start_lock:
            if self._graphics_running:
                pass
            else:
                self._graphics_ready.clear()
                self._graphics_running = True
                self._graphics_thread = threading.Thread(
                    target=self._graphics_bg_loop, daemon=True,
                    name=f"instruments-fps-{self.udid[:8]}"
                )
                self._graphics_thread.start()
        got = self._graphics_ready.wait(timeout=5)
        if not got:
            logger.warning(f"[Instruments/FPS] 等待首次数据超时 udid={self.udid}")

    def get_graphics(self) -> Dict:
        """读取最新 FPS/GPU 缓存，若未启动则自动 start_graphics()。"""
        with self._lock:
            running = self._graphics_running
        if not running:
            self.start_graphics()
        with self._lock:
            return dict(self._graphics_cache)

    # ── 公开接口 ──────────────────────────────────────────────────

    def start(self, pid: int = 0):
        """启动后台采集线程（幂等，多线程并发安全）。
        阻塞直到首次数据就绪（最多 8s），之后立即返回。
        """
        with self._start_lock:
            if self._running:
                # 已在运行：锁外等待数据就绪（不持锁阻塞）
                pass
            else:
                if pid:
                    self._watched_pids.add(pid)
                self._stop_event.clear()
                self._data_ready.clear()
                self._running = True
                self._bg_thread = threading.Thread(
                    target=self._bg_loop, daemon=True, name=f"instruments-{self.udid[:8]}"
                )
                self._bg_thread.start()

        # 锁外等待首次数据就绪（最多 8 秒）
        # 无论是新启动还是已在运行，都在这里等，不持锁
        got = self._data_ready.wait(timeout=8)
        if not got:
            logger.warning(f"[Instruments] 等待首次数据超时 udid={self.udid}")

    def stop(self):
        """停止所有后台采集线程"""
        self._stop_event.set()
        if self._bg_thread and self._bg_thread.is_alive():
            self._bg_thread.join(timeout=5)
        if self._graphics_thread and self._graphics_thread.is_alive():
            self._graphics_thread.join(timeout=5)
        with self._lock:
            self._running = False
            self._graphics_running = False
            self._cache = {"sys": {}, "procs": {}, "ts": 0.0}
            self._graphics_cache = {"fps": 0, "gpu": 0.0, "gpu_renderer": 0.0, "gpu_tiler": 0.0, "ts": 0.0}

    def get_latest(self, pid: int = 0) -> Dict:
        """
        读取最新缓存数据。
        若后台线程未启动，则自动 start()（内部等待首次数据就绪）。
        返回: {"proc": {...}, "sys": {...}}
        """
        with self._lock:
            running = self._running

        if not running:
            self.start(pid)   # 阻塞直到首次数据就绪

        with self._lock:
            sys_data  = dict(self._cache.get("sys", {}))
            proc_data = dict(self._cache.get("procs", {}).get(pid, {})) if pid else {}
            cache_ts  = self._cache["ts"]

        # 缓存超过 5 秒认为连接已断
        if cache_ts > 0 and (time.time() - cache_ts) > 5:
            logger.warning(f"[Instruments] 缓存超时 {time.time()-cache_ts:.1f}s，数据可能过期")

        return {"proc": proc_data, "sys": sys_data}

    def get_proc_cache(self) -> Dict:
        """返回当前缓存的全部进程数据副本（{pid: {proc_attrs}}）。

        供 include_child 递归聚合使用：调用方先 get_latest(pid) 确保后台
        采集已就绪，再读此缓存按 pid 聚合主进程及其后代。
        """
        with self._lock:
            return dict(self._cache.get("procs", {}))


# ── 全局管理：按 udid 获取 session ────────────────────────────────

def _get_instruments_session(udid: str) -> Optional["_InstrumentsSession"]:
    """获取（或创建）指定设备的 Instruments session"""
    if not _PY_IOS_DEVICE_AVAILABLE:
        return None
    return _InstrumentsSession.get(udid)


def ios_instruments_stop(udid: str):
    """主动停止指定设备的 Instruments 后台采集（设备断开时调用）"""
    with _InstrumentsSession._pool_lock:
        session = _InstrumentsSession._pool.pop(udid, None)
    if session:
        session.stop()
        logger.info(f"[Instruments] 已停止 udid={udid}")


# ─────────────────────────── 进程树 / 递归聚合辅助 ───────────────────────────

# iOS 进程树（{pid: ppid}）缓存，短 TTL，避免每轮重复 `go-ios ps`（较慢）。
_IOS_PID_TREE_CACHE: Dict[str, Dict[int, int]] = {}
_IOS_PID_TREE_TS: Dict[str, float] = {}
_IOS_PID_TREE_TTL = 5.0
_IOS_PID_TREE_LOCK = threading.Lock()


def _ios_build_pid_tree(udid: str) -> Dict[int, int]:
    """构建 {pid: ppid} 映射（来自 go-ios ps）。

    找不到父字段或缺省时返回 {}，由调用方降级为“仅主 PID”。
    """
    with _IOS_PID_TREE_LOCK:
        cached = _IOS_PID_TREE_CACHE.get(udid)
        ts = _IOS_PID_TREE_TS.get(udid, 0.0)
        if cached is not None and (time.time() - ts) < _IOS_PID_TREE_TTL:
            return cached

    tree: Dict[int, int] = {}
    raw = _run(["ps", "--udid", udid], timeout=8)
    if raw:
        for line in reversed(raw.strip().split("\n")):
            try:
                processes = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(processes, list):
                continue
            for p in processes:
                pid_val = p.get("Pid")
                if pid_val is None:
                    continue
                # go-ios ps 父字段可能为 ParentPid 或 Ppid
                ppid = p.get("ParentPid")
                if ppid is None:
                    ppid = p.get("Ppid")
                if ppid is None:
                    continue
                try:
                    tree[int(pid_val)] = int(ppid)
                except (TypeError, ValueError):
                    continue

    with _IOS_PID_TREE_LOCK:
        _IOS_PID_TREE_CACHE[udid] = tree
        _IOS_PID_TREE_TS[udid] = time.time()
    return tree


def _ios_descendant_pids(udid: str, pid: int) -> List[int]:
    """返回以 pid 为根的全部后代进程 pid（含自身），按进程树递归。

    - 无法构建父子树（ps 失败 / 无 ppid 字段）时仅返回 [pid]，
      保证主 PID 固定、不漂移。
    - 主 PID 始终位于结果首位且必被包含。
    """
    if not pid:
        return [pid]

    tree = _ios_build_pid_tree(udid)
    if not tree:
        return [pid]

    children_map: Dict[int, List[int]] = {}
    for p, ppid in tree.items():
        children_map.setdefault(ppid, []).append(p)

    result: List[int] = []
    seen: set = set()
    stack = [pid]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        result.append(cur)
        for c in children_map.get(cur, []):
            if c not in seen:
                stack.append(c)

    if not result:
        result = [pid]
    return result


def _ios_aggregate_proc_metrics(udid: str, pid: int, include_child: bool) -> Dict:
    """从 Instruments procs 缓存聚合主进程（+ 递归子进程）的 cpu/mem/thread。

    - 主 PID 始终固定包含；后代 pid 若不在缓存中则忽略，不影响主 PID。
    - include_child=False 时仅聚合主 PID。
    返回: {"cpuUsage", "physFootprint", "threadCount", "sys"}
    """
    session = _InstrumentsSession.get(udid)
    # 确保后台采集已启动并拿到首次数据（内部阻塞等待），同时拿到 sys 缓存
    data = session.get_latest(pid)
    sys_data = dict(data.get("sys", {}))
    procs = session.get_proc_cache()

    pids: List[int] = [pid]
    if include_child:
        pids = list(dict.fromkeys(_ios_descendant_pids(udid, pid)))
        if pid not in pids:
            pids = [pid] + pids  # 主 PID 固定首位，绝不漂移

    cpu = 0.0
    mem = 0.0
    threads = 0
    for p in pids:
        pd = procs.get(p)
        if not pd:
            continue
        try:
            cpu += float(pd.get("cpuUsage") or 0)
        except (TypeError, ValueError):
            pass
        try:
            mem += float(pd.get("physFootprint") or 0)
        except (TypeError, ValueError):
            pass
        tc = pd.get("threadCount")
        if tc is not None:
            try:
                threads += int(tc)
            except (TypeError, ValueError):
                pass

    return {
        "cpuUsage": cpu,
        "physFootprint": mem,
        "threadCount": threads,
        "sys": sys_data,
    }


# ─────────────────────────── CPU 采集 ───────────────────────────

async def ios_cpu(udid: str, pid: int = 0, include_child: bool = False, **kwargs) -> Optional[Dict]:
    """
    采集 CPU 使用率。
    优先使用 py-ios-device Instruments（进程级 cpuUsage + 系统总负载），
    并按 include_child 递归聚合主进程及其后代。
    fallback 到 go-ios sysmontap（仅系统总负载）。
    """

    def real_func():
        current_time = int(time.time())
        # 进程级 CPU 依赖 Instruments（按 PID 采集）。无 PID / 无 Instruments 源 /
        # 采集失败 → 对应字段为 None；真实采集到 0 保留 0。
        if _PY_IOS_DEVICE_AVAILABLE and pid:
            try:
                agg = _ios_aggregate_proc_metrics(udid, pid, include_child)
                cpu_usage = round(float(agg.get("cpuUsage") or 0), 2)
                return {
                    "cpu_usage": cpu_usage,
                    # Instruments 路径无独立系统负载源，沿用进程值；二者均依赖 PID
                    "cpu_usage_all": cpu_usage,
                    "cpu_core_num": None,  # Instruments 进程属性不含核心数源
                    "time": current_time,
                }
            except Exception as e:
                logger.warning(f"Instruments CPU 采集失败，fallback go-ios: {e}")
        # ── fallback: go-ios sysmontap（仅系统总负载，无法按 pid 拆分进程 CPU）──
        data = _read_sysmontap_sample(udid, skip_count=2)
        if data:
            return {
                "cpu_usage": None,  # sysmontap 无法按 PID 拆分进程 CPU
                "cpu_usage_all": round(data.get("cpu_total_load", 0), 2),
                "cpu_core_num": data.get("cpu_count", 0) or None,
                "time": current_time,
            }
        return {"cpu_usage": None, "cpu_usage_all": None, "cpu_core_num": None, "time": current_time}

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=25)


# ─────────────────────────── 内存采集 ───────────────────────────

async def ios_memory(udid: str, pid: int = 0, include_child: bool = False, **kwargs) -> Optional[Dict]:
    """
    采集内存使用。
    使用 py-ios-device Instruments:
      - 进程内存: physFootprint (bytes) → MB（按 include_child 递归聚合）
      - 系统总内存: physMemSize (pages * 16384) → MB
      - 系统空闲: vmFreeCount (pages * 16384) → MB
    """
    PAGE_SIZE = 16384  # iOS 页大小 16KB

    def real_func():
        current_time = int(time.time())
        # 进程内存依赖 PID + Instruments；系统总内存（不依赖 PID）与进程内存均可能
        # 因无源 / 无 PID / 采集失败而不可用 → None。真实采集到 0 保留 0。
        proc_mem = None
        mem_total = None
        if _PY_IOS_DEVICE_AVAILABLE:
            try:
                agg = _ios_aggregate_proc_metrics(udid, pid, include_child)
                sys = agg.get("sys", {})
                if pid:
                    phys = agg.get("physFootprint") or 0
                    proc_mem = round(phys / (1024 * 1024), 2)
                # 系统总内存 (pages → MB)
                phys_mem_pages = sys.get("physMemSize") or 0
                mem_total = round(phys_mem_pages * PAGE_SIZE / (1024 * 1024), 2)
            except Exception as e:
                logger.warning(f"Instruments Memory 采集失败: {e}")
        return {
            "process_memory_usage": proc_mem,
            "memory_total": mem_total,
            "time": current_time,
        }

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=25)


# ─────────────────────────── FPS 采集 ───────────────────────────

async def ios_fps(udid: str, pid: int = 0, **kwargs) -> Optional[Dict]:
    """
    采集 FPS（CoreAnimationFramesPerSecond）。
    通过 py-ios-device Instruments GraphicsOpengl channel 采集。
    手机屏幕亮起且有 UI 渲染时才有非零值。
    """
    def real_func():
        current_time = int(time.time())
        # FPS 依赖 Instruments Graphics 源（按屏幕渲染）。无源 / 采集失败 → fps 为
        # None；frames 始终为 []（无 FPS 明细时）。真实采集到 0 保留 0。
        if _PY_IOS_DEVICE_AVAILABLE:
            try:
                g = _InstrumentsSession.get(udid).get_graphics()
                # 仅当成功收到过帧数据（ts>0）才视为有效；否则无源/采集失败 → None
                if g.get("ts"):
                    return {"fps": g.get("fps"), "frames": [], "time": current_time}
                return {"fps": None, "frames": [], "time": current_time}
            except Exception as e:
                logger.warning(f"Instruments FPS 采集失败: {e}")
        return {"fps": None, "frames": [], "time": current_time}

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=15)


# ─────────────────────────── GPU 采集 ───────────────────────────

async def ios_gpu(udid: str, pid: int = 0, **kwargs) -> Dict:
    """
    采集 GPU 使用率。
    通过 py-ios-device Instruments GraphicsOpengl channel 采集：
      - gpu:          Device Utilization %（整体 GPU 利用率）
      - gpu_renderer: Renderer Utilization %
      - gpu_tiler:    Tiler Utilization %
    """
    def real_func():
        current_time = int(time.time())
        # GPU 依赖 Instruments Graphics 源。无源 / 采集失败 → 全部为 None；
        # 真实采集到 0 保留 0。仅当成功收到过数据（ts>0）才视为有效。
        if _PY_IOS_DEVICE_AVAILABLE:
            try:
                g = _InstrumentsSession.get(udid).get_graphics()
                if g.get("ts"):
                    return {
                        "gpu": g.get("gpu"),
                        "gpu_renderer": g.get("gpu_renderer"),
                        "gpu_tiler": g.get("gpu_tiler"),
                        "time": current_time,
                    }
                return {"gpu": None, "gpu_renderer": None, "gpu_tiler": None, "time": current_time}
            except Exception as e:
                logger.warning(f"Instruments GPU 采集失败: {e}")
        return {"gpu": None, "gpu_renderer": None, "gpu_tiler": None, "time": current_time}

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=15)


# ─────────────────────────── 进程信息 ───────────────────────────

async def ios_process_info(udid: str, bundle_id: str = "", pid: int = 0, include_child: bool = False, **kwargs) -> Dict:
    """
    采集进程信息（线程数）。
    优先从 Instruments sysmontap 缓存读取 threadCount，并按 include_child
    递归聚合主进程及其后代（< 1ms）。
    fallback 到 go-ios ps（较慢，超时 8s）。
    """
    def real_func():
        current_time = int(time.time())
        num_threads = None
        # 线程数依赖 PID + Instruments（或 go-ios ps）；无 PID / 无源 / 失败 → None。
        # iOS 不支持 handles → num_handles 始终为 None。

        # ── 优先：Instruments 缓存（threadCount 字段，递归聚合）──
        if _PY_IOS_DEVICE_AVAILABLE and pid:
            try:
                agg = _ios_aggregate_proc_metrics(udid, pid, include_child)
                num_threads = int(agg.get("threadCount") or 0)
                return {"time": current_time, "num_threads": num_threads, "num_handles": None}
            except Exception:
                pass

        # ── fallback：go-ios ps（超时 8s）────────────────────────
        if bundle_id:
            raw = _run(["ps", "--udid", udid], timeout=8)
            if raw:
                for line in reversed(raw.strip().split("\n")):
                    try:
                        processes = json.loads(line)
                        if isinstance(processes, list):
                            app_procs = [p for p in processes
                                         if bundle_id.split(".")[-1].lower() in p.get("Name", "").lower()]
                            num_threads = len(app_procs) if app_procs else 0
                            break
                    except json.JSONDecodeError:
                        continue

        return {"time": current_time, "num_threads": num_threads, "num_handles": None}

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=12)


# ─────────────────────────── 网络 IO ───────────────────────────

async def ios_network_io(udid: str, pid: int = 0, include_child: bool = False, **kwargs) -> Dict:
    """
    采集网络 I/O。

    注意：py-ios-device Instruments 的 netBytesIn / netBytesOut 为系统级
    整机累计值，并非按 PID 拆分的进程网络字节数；Instruments 也无可靠的
    按 PID 网络字节源。iOS 真机不存在可信的“按进程网络”来源，按规范
    “无进程网络/磁盘源为 None”，这里所有字段统一为 None，字段始终存在。
    若后续引入按 PID 网络采集能力，可在此聚合主进程与递归子进程。
    """
    current_time = int(time.time())
    return {
        "net_sent_rate": None,
        "net_recv_rate": None,
        "net_sent": None,
        "net_recv": None,
        "time": current_time,
    }


# ─────────────────────────── 磁盘 IO ───────────────────────────

async def ios_disk_io(udid: str, pid: int = 0, include_child: bool = False, **kwargs) -> Dict:
    """
    采集磁盘 I/O。

    注意：py-ios-device Instruments 的 diskBytesRead / diskBytesWritten 为
    系统级整机累计值，并非按 PID 拆分的进程磁盘字节数。虽然 sysmontap 进程
    属性中存在 diskBytesRead/Written，但其聚合依赖可靠的进程父子关系，且
    当前策略以“不冒充进程数据”为准；iOS 真机不存在可信的“按进程磁盘”来源，
    按规范“无进程网络/磁盘源为 None”，这里所有字段统一为 None，字段始终存在。
    若后续引入可靠的按 PID 磁盘聚合，可在此聚合主进程与递归子进程。
    """
    current_time = int(time.time())
    return {
        "disk_read_rate": None,
        "disk_write_rate": None,
        "disk_read": None,
        "disk_write": None,
        "time": current_time,
    }


# ─────────────────────────── 电池信息 ───────────────────────────

async def ios_battery(udid: str, **kwargs) -> Dict:
    """
    采集电池信息
    优先使用 batteryregistry（含电流、温度、电量），
    失败则回退到 batterycheck（仅电量）。
    """
    def real_func():
        # 优先 batteryregistry：InstantAmperage(mA), Temperature(0.01°C), CurrentCapacity(%)
        data = _run_json(["batteryregistry", "--udid", udid]) or {}
        if data.get("CurrentCapacity") is not None:
            # Temperature 单位是 0.01°C（如 3659 = 36.59°C）
            temp = round(data.get("Temperature", 0) / 100.0, 2)
            # InstantAmperage 单位是 mA（正值=充电，负值=放电）
            current = round(data.get("InstantAmperage", 0) / 1.0, 2)
            return {
                "time": int(time.time()),
                "battery_level": data.get("CurrentCapacity", 0),
                "battery_temperature": temp,
                "battery_current": current,
            }
        # 回退 batterycheck
        data2 = _run_json(["batterycheck", "--udid", udid]) or {}
        return {
            "time": int(time.time()),
            "battery_level": data2.get("BatteryCurrentCapacity", 0),
            "battery_temperature": 0,
            "battery_current": 0,
        }

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=15)


# ─────────────────────────── 截图 ───────────────────────────

async def ios_screenshot(udid: str, save_dir: str = None, **kwargs):
    """设备截图"""
    def real_func():
        if not GO_IOS_PATH:
            return None
        if save_dir:
            screenshot_dir = Path(save_dir) / "screenshot"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            out_path = str(screenshot_dir / f"{int(time.time())}.png")
        else:
            out_path = f"/tmp/ios_screenshot_{int(time.time())}.png"

        # go-ios screenshot 成功信息输出到 stderr，直接执行并检查文件
        try:
            subprocess.run(
                [GO_IOS_PATH, "screenshot", "--udid", udid, "--output", out_path],
                capture_output=True, timeout=15, encoding="utf-8"
            )
        except Exception as e:
            logger.error(f"iOS 截图失败: {e}")
            return None

        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            if not save_dir:
                with open(out_path, "rb") as f:
                    data = f.read()
                os.remove(out_path)
                return data
            return True  # save_dir 模式下文件已保存
        return None

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=20)


# ─────────────────────────── 性能采集入口 ───────────────────────────

async def ios_perf(udid: str, bundle_id: str, pid: int, save_dir: str, include_child: bool = False):
    """
    iOS 性能采集入口
    udid/bundle_id/pid 由上层传入，bundle_id 和 pid 可以自动获取

    支持的指标（py-ios-device Instruments + go-ios）：
    - CPU 使用率（Instruments sysmontap，进程级）
    - 内存使用（Instruments sysmontap，physFootprint）
    - 网络 IO（Instruments sysmontap，系统级）
    - 磁盘 IO（Instruments sysmontap，系统级）
    - FPS（Instruments GraphicsOpengl，CoreAnimationFramesPerSecond）
    - GPU（Instruments GraphicsOpengl，Device/Renderer/Tiler Utilization）
    - 电池（go-ios batteryregistry）
    - 截图（go-ios screenshot）
    - 进程信息（go-ios ps，线程数）
    """
    # 自动获取 bundle_id 和 pid
    if not bundle_id or not pid:
        fg = _get_foreground_app(udid)
        if fg:
            if not bundle_id:
                bundle_id = fg.get("bundle_id", "")
            if not pid:
                pid = fg.get("pid", 0)

    if not pid and bundle_id:
        pid = _find_pid_by_bundle(udid, bundle_id)

    logger.info(f"iOS 性能采集: udid={udid}, bundle_id={bundle_id}, pid={pid}")

    # ── 预热 Instruments 后台采集线程（sysmontap + graphics）──────
    # 连接策略（iOS 版本 → USB/tunnel）统一在 _InstrumentsSession._make_lockdown
    # 中处理，避免与 ios_perf 重复启动 tunnel。
    # 必须在 Monitor 启动前完成，否则第一轮采集会因等待建连而超时。
    if _PY_IOS_DEVICE_AVAILABLE:
        session = _InstrumentsSession.get(udid)
        logger.info(f"[ios_perf] 预热 Instruments sysmontap...")
        await asyncio.to_thread(session.start, pid)
        logger.info(f"[ios_perf] 预热 Instruments graphics (FPS/GPU)...")
        await asyncio.to_thread(session.start_graphics)
        logger.info(f"[ios_perf] Instruments 预热完成，开始采集")

    monitors = {
        "cpu": Monitor(ios_cpu,
                       udid=udid, pid=pid, include_child=include_child,
                       monitor_name="cpu",
                       key_value=["time", "cpu_usage(%)", "cpu_usage_all(%)", "cpu_core_num(个)"],
                       save_dir=save_dir),
        "memory": Monitor(ios_memory,
                          udid=udid, pid=pid, include_child=include_child,
                          monitor_name="memory",
                          key_value=["time", "process_memory_usage(M)", "memory_total(M)"],
                          save_dir=save_dir),
        "process_info": Monitor(ios_process_info,
                                udid=udid, bundle_id=bundle_id, pid=pid, include_child=include_child,
                                monitor_name="process_info",
                                key_value=["time", "num_threads(个)", "num_handles(个)"],
                                save_dir=save_dir),
        "fps": Monitor(ios_fps,
                       udid=udid, pid=pid,
                       monitor_name="fps",
                       key_value=["time", "fps(帧)", "frames"],
                       save_dir=save_dir),
        "gpu": Monitor(ios_gpu,
                       udid=udid, pid=pid,
                       monitor_name="gpu",
                       key_value=["time", "gpu(%)", "gpu_renderer(%)", "gpu_tiler(%)"],
                       save_dir=save_dir),
        "disk_io": Monitor(ios_disk_io,
                           udid=udid, pid=pid, include_child=include_child,
                           monitor_name="disk_io",
                           key_value=["time", "disk_read_rate(MB/s)", "disk_write_rate(MB/s)",
                                      "disk_read(字节)", "disk_write(字节)"],
                           save_dir=save_dir),
        "network_io": Monitor(ios_network_io,
                              udid=udid, pid=pid, include_child=include_child,
                              monitor_name="network_io",
                              key_value=["time", "net_sent_rate(MB/s)", "net_recv_rate(MB/s)",
                                         "net_sent(字节)", "net_recv(字节)"],
                              save_dir=save_dir),
        "battery": Monitor(ios_battery,
                           udid=udid,
                           monitor_name="battery",
                           key_value=["time", "battery_level(%)", "battery_temperature(℃)",
                                      "battery_current(mA)"],
                           save_dir=save_dir),
        "screenshot": Monitor(ios_screenshot,
                              udid=udid,
                              save_dir=save_dir, is_out=False)
    }
    run_monitors = [monitor.run() for name, monitor in monitors.items()]
    await asyncio.gather(*run_monitors)
