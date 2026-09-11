# coding:utf-8
"""
HarmonyOS 性能测试模块
通过 hdc（HarmonyOS Device Connector）命令行工具采集设备性能数据。

依赖：hdc 工具需在 PATH 中，或通过 HDC_PATH 环境变量指定路径。
安装方式：随 DevEco Studio 或 HarmonyOS SDK 一起安装，通常位于 SDK/toolchains/ 目录。

支持的指标：
  - CPU 使用率（/proc/stat + /proc/<pid>/stat）
  - 内存使用（hidumper --mem 或 /proc/<pid>/status）
  - 网络 IO（按固定进程树 UID 聚合 /proc/net/xt_qtaguid/stats）
  - 磁盘 IO（/proc/<pid>/io 两次采样差值）
  - 电池信息（hidumper -s BatteryService）
  - FPS（无可信进程级数据源时返回 None）
  - 截图（hdc shell snapshot_display）
  - 进程信息（线程数、FD 数）
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import threading
from pathlib import Path
from typing import Optional, Dict, List

from client_perf.log import log as logger
from client_perf.core.monitor import Monitor

# ─────────────────────────── hdc 路径 ───────────────────────────

def _resolve_hdc_path() -> Optional[str]:
    """解析 hdc 路径，兼容终端 PATH 未传入 IDE/桌面应用的场景。"""
    configured = os.environ.get("HDC_PATH")
    if configured:
        return str(Path(configured).expanduser())

    discovered = shutil.which("hdc")
    if discovered:
        return discovered

    # GUI/IDE 启动的进程通常不会加载交互式 shell 配置。只探测常见且
    # 明确的用户级安装位置，不启动 shell，也不修改当前进程的 PATH。
    candidates = [
        Path.home() / ".hdc" / "hdc" / "hdc",
        Path("/usr/local/bin/hdc"),
        Path("/opt/homebrew/bin/hdc"),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


HDC_PATH = _resolve_hdc_path()

if not HDC_PATH:
    logger.warning("hdc 未找到，HarmonyOS 性能测试不可用。请安装 DevEco Studio 或 HarmonyOS SDK 并将 hdc 加入 PATH，或设置 HDC_PATH 环境变量")

HDC_AVAILABLE = bool(HDC_PATH)


def _hdc(args: list, serial: str = None, timeout: int = 15) -> Optional[str]:
    """
    执行 hdc 命令，返回输出字符串。
    若指定 serial，则加 -t <serial> 参数。
    """
    if not HDC_PATH:
        return None
    cmd = [HDC_PATH]
    if serial:
        cmd += ["-t", serial]
    cmd += args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8"
        )
        out = result.stdout.strip()
        if out:
            return out
        err = result.stderr.strip()
        if err:
            return err
        return None
    except subprocess.TimeoutExpired:
        logger.error(f"hdc {' '.join(args)} 超时")
    except Exception as e:
        logger.error(f"hdc {' '.join(args)} 异常: {e}")
    return None


def _shell(serial: str, cmd: str, timeout: int = 15) -> str:
    """在鸿蒙设备上执行 shell 命令，返回输出（空字符串表示失败）"""
    out = _hdc(["shell", cmd], serial=serial, timeout=timeout)
    return out or ""


def print_json(msg):
    logger.info(json.dumps(msg, ensure_ascii=False))


# ─────────────────────────── 设备管理 ───────────────────────────

def _clean_shell_output(val: str) -> str:
    """
    清理 hdc shell 输出中的日志噪音。
    hdc 经常在输出末尾追加 [W] / [E] 等级别的日志行，需要去掉。
    """
    if not val:
        return ""
    lines = val.strip().split("\n")
    # 过滤掉 hdc 日志行（以 [W]、[E]、[I] 等开头的行）
    clean_lines = [l for l in lines if not re.match(r'^\[(?:W|E|I|D)\]', l.strip())]
    return "\n".join(clean_lines).strip()


def _is_valid_prop_value(val: str) -> bool:
    """
    判断 param get 返回的值是否有效。
    过滤掉错误信息等无效输出。
    """
    if not val:
        return False
    lower = val.lower()
    # 过滤常见的错误/无效输出关键词
    error_keywords = ["not found", "inaccessible", "fail", "error",
                      "permission denied", "no such"]
    return not any(kw in lower for kw in error_keywords)


_DEVICE_PROPERTY_CACHE_TTL = 30.0
_DEVICE_PROPERTY_CACHE: Dict[str, tuple[float, Dict[str, str]]] = {}
_DEVICE_PROPERTY_CACHE_LOCK = threading.Lock()

_DEVICE_PROP_KEYS = {
    "model": (
        "const.product.model",
        "const.product.name",
        "ro.product.model",
    ),
    "brand": (
        "const.product.brand",
        "const.product.manufacturer",
        "ro.product.brand",
    ),
    "harmony_version": (
        "const.ohos.fullname",
        "const.build.version.release",
        "ro.build.version.release",
    ),
    "sdk_version": (
        "const.ohos.apiversion",
        "const.build.version.sdk",
        "ro.build.version.sdk",
    ),
}


def _property_candidates(key: str) -> tuple[str, ...]:
    """返回去重后的属性候选名，HarmonyOS 原生 const.* 属性优先。"""
    for field, candidates in _DEVICE_PROP_KEYS.items():
        if key == field or key in candidates:
            return candidates
    ohos_key = key.replace("ro.product.", "const.product.").replace(
        "ro.build.", "const.build."
    )
    return tuple(dict.fromkeys((ohos_key, key)))


def _get_device_prop(serial: str, key: str, timeout: int = 5) -> str:
    """获取单个设备属性；兼容调用使用，候选属性不会重复查询。"""
    for candidate in _property_candidates(key):
        val = _clean_shell_output(
            _shell(serial, f"param get {candidate} 2>/dev/null", timeout=timeout)
        )
        if _is_valid_prop_value(val):
            return val
    return ""


def _get_device_properties(serial: str, timeout: int = 5) -> Dict[str, str]:
    """通过一次 hdc shell 往返批量读取设备发现所需属性。

    每个候选值前输出唯一标记，避免依赖空行是否被 hdc 保留。设备端仍会依次
    执行若干轻量 ``param get``，但主机只创建一个 hdc 进程和一次连接。成功结果
    短时缓存，避免前端连续刷新设备列表时重复获取静态属性。
    """
    now = time.monotonic()
    with _DEVICE_PROPERTY_CACHE_LOCK:
        cached = _DEVICE_PROPERTY_CACHE.get(serial)
        if cached and now - cached[0] < _DEVICE_PROPERTY_CACHE_TTL:
            return dict(cached[1])

    markers = []
    commands = []
    for field, candidates in _DEVICE_PROP_KEYS.items():
        for index, candidate in enumerate(candidates):
            marker = f"__CLIENT_PERF_PROP_{field}_{index}__"
            markers.append((marker, field))
            commands.extend((f"echo {marker}", f"param get {candidate} 2>/dev/null"))

    output = _shell(serial, "; ".join(commands), timeout=timeout)
    if not output:
        return {}

    marker_fields = dict(markers)
    values: Dict[str, str] = {}
    current_field = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line in marker_fields:
            current_field = marker_fields[line]
            continue
        if current_field and current_field not in values:
            value = _clean_shell_output(line)
            if _is_valid_prop_value(value):
                values[current_field] = value
            current_field = None

    if values:
        with _DEVICE_PROPERTY_CACHE_LOCK:
            _DEVICE_PROPERTY_CACHE[serial] = (now, dict(values))
    return values


def _prune_device_property_cache(active_serials: List[str]) -> None:
    """移除已断开设备的缓存，不让历史设备条目持续驻留。"""
    active = set(active_serials)
    with _DEVICE_PROPERTY_CACHE_LOCK:
        for serial in list(_DEVICE_PROPERTY_CACHE):
            if serial not in active:
                _DEVICE_PROPERTY_CACHE.pop(serial, None)


def _parse_hdc_targets(output: str) -> List[str]:
    """解析 ``hdc list targets``，忽略日志、错误信息、表头和重复设备。"""
    serials = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("[") or "empty" in line.lower():
            continue
        parts = line.split()
        serial = parts[0]
        lower = line.lower()
        if any(token in lower for token in ("targets", "error", "failed", "failure", "daemon", "server")):
            continue
        if serial.lower().rstrip(":") in {"targets", "target", "warning", "info"}:
            continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", serial):
            continue
        if serial not in serials:
            serials.append(serial)
    return serials


def get_harmony_devices() -> List[Dict]:
    """获取已连接设备；列表一次查询，每台设备只追加一次属性查询。"""
    if not HDC_AVAILABLE:
        return []
    try:
        output = _hdc(["list", "targets"], timeout=5)
        if not output:
            return []

        serials = _parse_hdc_targets(output)
        _prune_device_property_cache(serials)
        devices = []
        for serial in serials:
            properties = _get_device_properties(serial, timeout=5)
            model = properties.get("model", "Unknown")
            brand = properties.get("brand", "Unknown")
            os_version = properties.get("harmony_version", "Unknown")
            sdk_version = properties.get("sdk_version", "Unknown")
            devices.append({
                "serial": serial,
                "model": model,
                "brand": brand,
                "harmony_version": os_version,
                "sdk_version": sdk_version,
                "status": "online",
                "device_type": "harmony",
                "name": f"{brand} {model}",
            })
        return devices
    except Exception as e:
        logger.error(f"获取 HarmonyOS 设备列表失败: {e}")
        return []


# ─────────────────────────── 设备信息 ───────────────────────────

async def harmony_sys_info(serial: str) -> Dict:
    """获取 HarmonyOS 设备系统信息"""
    def real_func():
        model = _get_device_prop(serial, "ro.product.model") or "Unknown"
        brand = _get_device_prop(serial, "ro.product.brand") or "Unknown"
        os_version = _get_device_prop(serial, "ro.build.version.release") or "Unknown"

        # CPU 核心数（优先 nproc，/proc/cpuinfo 在部分鸿蒙设备上无权限）
        cpu_cores_out = _clean_shell_output(_shell(serial, "nproc 2>/dev/null"))
        if not cpu_cores_out or not cpu_cores_out.isdigit():
            cpu_cores_out = _clean_shell_output(
                _shell(serial, "cat /proc/cpuinfo 2>/dev/null | grep processor | wc -l"))
        cpu_cores = int(cpu_cores_out) if cpu_cores_out and cpu_cores_out.isdigit() else 0

        # 内存总量
        mem_info = _clean_shell_output(_shell(serial, "cat /proc/meminfo 2>/dev/null | grep MemTotal"))
        mem_kb = int(re.search(r'(\d+)', mem_info).group(1)) if mem_info else 0
        mem_gb = round(mem_kb / 1024 / 1024, 1)

        # 存储
        disk_info = _shell(serial, "df /data | tail -1").strip()
        disk_parts = disk_info.split()
        disk_total_gb = round(int(disk_parts[1]) / 1024 / 1024, 1) if len(disk_parts) > 1 else 0

        res = {
            "platform": "HarmonyOS",
            "computer_name": f"{brand} {model}",
            "time": time.time(),
            "cpu_cores": cpu_cores,
            "ram": f"{mem_gb}G",
            "rom": f"{disk_total_gb}G",
            "harmony_version": os_version,
            "serial": serial,
        }
        print_json(res)
        return res

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=15)


# ─────────────────────────── 进程/应用列表 ───────────────────────────

def _running_package_pids(serial: str) -> Optional[Dict[str, int]]:
    """通过一次进程快照返回包名到 PID 的映射；命令不可用时返回 None。"""
    output = _shell(serial, "ps -A -o PID,NAME 2>/dev/null")
    if not output or "error" in output.lower() or "not found" in output.lower():
        return None

    pids: Dict[str, int] = {}
    parsed_process = False
    for line in output.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        parsed_process = True
        pid = int(parts[0])
        process_name = parts[1].strip().split()[0]
        if not process_name:
            continue
        # 多进程应用常见形态为 com.example.app:service，主包仍视为运行中。
        package_name = process_name.split(":", 1)[0]
        pids.setdefault(package_name, pid)

    return pids if parsed_process else None


def _package_entry(pkg_name: str, running_pids: Optional[Dict[str, int]], serial: str) -> Dict:
    """构造应用条目；仅在进程快照不可用时兼容回退到单包 pidof。"""
    if running_pids is None:
        pid_output = _shell(serial, f"pidof {pkg_name} 2>/dev/null").strip()
        pid = int(pid_output.split()[0]) if pid_output and pid_output.split()[0].isdigit() else 0
    else:
        pid = running_pids.get(pkg_name, 0)
    return {
        "package_name": pkg_name,
        "pid": pid,
        "name": pkg_name,
        "running": pid > 0,
        "bundle_id": pkg_name,
    }


async def harmony_packages(serial: str) -> List[Dict]:
    """获取 HarmonyOS 设备上已安装的应用包名列表。"""
    def real_func():
        packages = []

        # 一次获取全部进程，避免针对每个应用各启动一次 hdc/pidof。
        running_pids = _running_package_pids(serial)

        # 方法1: bm dump -a（HarmonyOS 标准方式）
        output = _shell(serial, "bm dump -a 2>/dev/null", timeout=20)
        if output:
            for line in output.strip().split("\n"):
                line = line.strip()
                # 过滤掉非包名行（空行、表头、提示信息等）
                if not line or line.startswith("ID") or ":" in line[:3]:
                    continue
                # 跳过包含错误信息或非包名格式的行
                if "error" in line.lower() or "not found" in line.lower():
                    continue
                if "inaccessible" in line.lower():
                    continue
                # 包名通常是类似 com.xxx.yyy 的格式
                pkg_name = line.split()[0] if line.split() else line
                if not pkg_name or len(pkg_name) < 2:
                    continue
                packages.append(_package_entry(pkg_name, running_pids, serial))

        # 方法2: bm dump --bundle-name 的另一种解析方式
        if not packages:
            output2 = _shell(serial, "bm dump-shared-dependencies -a 2>/dev/null", timeout=20)
            if not output2:
                # 方法3: 最后尝试 aa dump（获取正在运行的 Ability）
                output2 = _shell(serial, "aa dump -a 2>/dev/null", timeout=20)
            if output2:
                # 从输出中提取包名（com.xxx.yyy 格式）
                bundle_names = set()
                for match in re.finditer(r'(com\.[a-zA-Z0-9_.]+)', output2):
                    bundle_names.add(match.group(1))
                for pkg_name in sorted(bundle_names):
                    packages.append(_package_entry(pkg_name, running_pids, serial))

        packages.sort(key=lambda x: (-int(x['running']), x['name']))
        return packages

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=30)


# ─────────────────────────── CPU 采集 ───────────────────────────

def _read_proc_stat(serial: str) -> Optional[Dict]:
    """读取 /proc/stat 获取系统 CPU 时间"""
    output = _shell(serial, "cat /proc/stat | head -1").strip()
    parts = output.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return None
    try:
        user = int(parts[1])
        nice = int(parts[2])
        system = int(parts[3])
        idle = int(parts[4])
        iowait = int(parts[5]) if len(parts) > 5 else 0
        irq = int(parts[6]) if len(parts) > 6 else 0
        softirq = int(parts[7]) if len(parts) > 7 else 0
        total = user + nice + system + idle + iowait + irq + softirq
        return {"user": user, "nice": nice, "system": system, "idle": idle,
                "iowait": iowait, "irq": irq, "softirq": softirq, "total": total}
    except (ValueError, IndexError):
        return None


def _read_pid_stat(serial: str, pid: int) -> Optional[Dict]:
    """读取 /proc/<pid>/stat 获取进程 CPU 时间"""
    output = _shell(serial, f"cat /proc/{pid}/stat 2>/dev/null").strip()
    if not output:
        return None
    parts = output.split()
    if len(parts) < 15:
        return None
    try:
        utime = int(parts[13])
        stime = int(parts[14])
        return {"utime": utime, "stime": stime, "total": utime + stime}
    except (ValueError, IndexError):
        return None


def _discover_tree_pids(serial: str, main_pid: int) -> List[int]:
    """返回以 main_pid 为根的进程树 PID 列表（含 main_pid）。

    每轮调用重新快照 ``ps -A`` 的 PID/PPID 并 BFS 发现后代，以适配子进程动态
    变化。main_pid 无效或快照不可用时仅返回 [main_pid]。
    """
    if not main_pid:
        return []
    output = _shell(serial, "ps -A -o PID,PPID 2>/dev/null")
    if not output:
        return [main_pid]
    children_map: Dict[int, List[int]] = {}
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            children_map.setdefault(int(parts[1]), []).append(int(parts[0]))
    result: List[int] = []
    seen: set = set()
    queue = [main_pid]
    while queue:
        p = queue.pop(0)
        if p in seen:
            continue
        seen.add(p)
        result.append(p)
        for child in children_map.get(p, []):
            if child not in seen:
                queue.append(child)
    return result or [main_pid]


async def harmony_cpu(serial: str, pid: int = 0, package_name: str = "", include_child: bool = False, **kwargs) -> Dict:
    """
    采集 HarmonyOS 进程 CPU 使用率
    使用 /proc/stat 和 /proc/<pid>/stat 两次采样计算。
    include_child=True 时每轮递归发现进程树后代 PID 并聚合 CPU 时间。
    """
    def real_func():
        current_time = int(time.time())

        # 采样时主 PID 已固定；include_child 模式下仅在入口通过 package_name 找一次主 PID。
        target_pid = pid
        if not target_pid and package_name and not include_child:
            pid_output = _shell(serial, f"pidof {package_name} 2>/dev/null").strip()
            if pid_output:
                parts = pid_output.split()
                target_pid = int(parts[0]) if parts[0].isdigit() else 0

        # CPU 核心数。部分 HarmonyOS 设备禁止读取 /proc/cpuinfo，优先使用 nproc。
        cpu_cores = 0
        for command in ("nproc 2>/dev/null", "cat /proc/cpuinfo | grep processor | wc -l"):
            cpu_cores_out = _shell(serial, command).strip()
            if cpu_cores_out.isdigit() and int(cpu_cores_out) > 0:
                cpu_cores = int(cpu_cores_out)
                break
        if cpu_cores <= 0:
            cpu_cores = 1

        if not target_pid:
            # 无 PID：进程级 CPU 指标不可用；cpu_core_num 为设备级信息保留真实值
            return {"cpu_usage": None, "cpu_usage_all": None, "cpu_core_num": cpu_cores, "time": current_time}

        # include_child 时每轮重新发现进程树（主 PID 固定，后代动态发现）。
        tree_pids = _discover_tree_pids(serial, target_pid) if include_child else [target_pid]

        # 第一次采样
        sys_stat1 = _read_proc_stat(serial)
        pid_stats1 = {p: _read_pid_stat(serial, p) for p in tree_pids}

        time.sleep(1)

        # 第二次采样
        sys_stat2 = _read_proc_stat(serial)
        pid_stats2 = {p: _read_pid_stat(serial, p) for p in tree_pids}

        # 系统整体 CPU：依赖 /proc/stat，读取失败或无法计算差值时为 None；
        # 真实采集到的 0（完全空闲）保留 0。
        cpu_usage_all = None
        if sys_stat1 and sys_stat2:
            sys_delta = sys_stat2["total"] - sys_stat1["total"]
            if sys_delta > 0:
                sys_idle_delta = sys_stat2["idle"] - sys_stat1["idle"]
                cpu_usage_all = round((1 - sys_idle_delta / sys_delta) * 100, 2)

        # 进程树 CPU：依赖 /proc/stat 与 /proc/<pid>/stat，任一源缺失（权限/进程结束）时为 None；
        # 真实采集到的 0（采样窗口内无 CPU 占用）保留 0。
        cpu_usage = None
        if sys_stat1 and sys_stat2:
            sys_delta = sys_stat2["total"] - sys_stat1["total"]
            if sys_delta > 0:
                pid_delta = 0
                any_readable = False
                for p in tree_pids:
                    s1 = pid_stats1.get(p)
                    s2 = pid_stats2.get(p)
                    if s1 and s2:
                        any_readable = True
                        pid_delta += s2["total"] - s1["total"]
                if any_readable:
                    cpu_usage = round(pid_delta / sys_delta * 100 * cpu_cores, 2)

        res = {
            "cpu_usage": cpu_usage,
            "cpu_usage_all": cpu_usage_all,
            "cpu_core_num": cpu_cores,
            "time": current_time
        }
        print_json(res)
        return res

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=20)


# ─────────────────────────── 内存采集 ───────────────────────────

async def harmony_memory(serial: str, pid: int = 0, package_name: str = "", include_child: bool = False, **kwargs) -> Dict:
    """
    采集 HarmonyOS 进程内存使用。
    include_child=True 时每轮递归发现进程树后代 PID，按 /proc/<pid>/status 的
    VmRSS 求和（不依赖 package_name 重新解析，主 PID 固定）。
    """
    def real_func():
        current_time = int(time.time())

        target_pid = pid
        if not target_pid and package_name and not include_child:
            pid_output = _shell(serial, f"pidof {package_name} 2>/dev/null").strip()
            if pid_output:
                parts = pid_output.split()
                target_pid = int(parts[0]) if parts[0].isdigit() else 0

        # include_child：跨进程树聚合 VmRSS，主 PID 固定、不回退 package_name。
        if include_child:
            if not target_pid:
                # 无 PID：进程级内存指标不可用
                return {"process_memory_usage": None, "time": current_time}
            tree_pids = _discover_tree_pids(serial, target_pid)
            memory_kb = 0
            all_readable = True
            for p in tree_pids:
                status_output = _shell(serial, f"cat /proc/{p}/status 2>/dev/null")
                if not status_output or "No such file" in status_output or "Permission denied" in status_output:
                    all_readable = False
                    break
                match = re.search(r'VmRSS:\s+(\d+)\s+kB', status_output)
                if match:
                    memory_kb += int(match.group(1))
                else:
                    # 源缺失（无可解析 VmRSS）
                    all_readable = False
                    break
            res = {"process_memory_usage": (round(memory_kb / 1024.0, 2) if all_readable else None),
                   "time": current_time}
            print_json(res)
            return res

        # include_child=False：保持原有单进程逻辑（hidumper --mem 或 /proc/<pid>/status）。
        target = package_name if package_name else str(pid)
        if not target or target == "0":
            # 无目标：进程级内存指标不可用
            return {"process_memory_usage": None, "time": current_time}

        memory_mb = None
        got_value = False

        # 方法1: hidumper --mem（鸿蒙专用，获取 PSS）
        if package_name:
            try:
                output = _shell(serial, f"hidumper --mem {package_name} 2>/dev/null", timeout=10)
                if output:
                    # 查找 Total PSS 行
                    match = re.search(r'Total\s+PSS[:\s]+(\d+)', output, re.IGNORECASE)
                    if match:
                        memory_mb = int(match.group(1)) / 1024.0
                        got_value = True
                    else:
                        # 查找 Pss Total 行
                        match = re.search(r'Pss\s+Total[:\s]+(\d+)', output, re.IGNORECASE)
                        if match:
                            memory_mb = int(match.group(1)) / 1024.0
                            got_value = True
            except Exception as e:
                logger.warning(f"hidumper --mem 失败: {e}")

        # 方法2: /proc/<pid>/status（备用）；源缺失时为 None
        if not got_value:
            target_pid = pid
            if not target_pid and package_name:
                pid_output = _shell(serial, f"pidof {package_name} 2>/dev/null").strip()
                if pid_output:
                    parts = pid_output.split()
                    target_pid = int(parts[0]) if parts[0].isdigit() else 0
            if target_pid:
                try:
                    status_output = _shell(serial, f"cat /proc/{target_pid}/status 2>/dev/null")
                    if not status_output or "No such file" in status_output or "Permission denied" in status_output:
                        memory_mb = None
                    else:
                        match = re.search(r'VmRSS:\s+(\d+)\s+kB', status_output)
                        memory_mb = (int(match.group(1)) / 1024.0) if match else None
                except Exception as e:
                    logger.warning(f"/proc/pid/status 读取失败: {e}")
                    memory_mb = None
            else:
                memory_mb = None

        res = {"process_memory_usage": (round(memory_mb, 2) if memory_mb is not None else None),
               "time": current_time}
        print_json(res)
        return res

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=20)


# ─────────────────────────── FPS 采集 ───────────────────────────

async def harmony_fps(serial: str, pid: int = 0, package_name: str = "", **kwargs) -> Dict:
    """返回 HarmonyOS 进程级 FPS。

    当前公开的 RenderService screen 输出反映屏幕刷新率，而不是目标进程的
    实际渲染帧率。为避免把 120 Hz 等刷新率误报为应用 FPS，在没有可信的
    进程级数据源时相关数值字段返回 None；frames 始终为 []。
    """
    return {
        "type": "fps",
        "fps": None,
        "frames": [],
        "time": int(time.time()),
    }


# ─────────────────────────── GPU 采集 ───────────────────────────

async def harmony_gpu(serial: str, **kwargs) -> Dict:
    """
    采集 HarmonyOS GPU 使用率
    尝试读取 /sys 节点（与 Android 类似，鸿蒙底层共用 Linux 内核）
    """
    def real_func():
        start_time = int(time.time())
        gpu_usage = None

        # 尝试 Qualcomm GPU 节点
        gpu_paths = [
            "/sys/class/kgsl/kgsl-3d0/gpubusy",
            "/sys/class/kgsl/kgsl-3d0/gpu_busy_percentage",
        ]
        for path in gpu_paths:
            output = _shell(serial, f"cat {path} 2>/dev/null").strip()
            if output and "No such file" not in output and "Permission denied" not in output:
                try:
                    parts = output.split()
                    if len(parts) == 2:
                        busy = int(parts[0])
                        total = int(parts[1])
                        if total > 0:
                            gpu_usage = round((busy / total) * 100, 2)
                            break
                    elif len(parts) == 1:
                        gpu_usage = float(parts[0].replace('%', ''))
                        break
                except (ValueError, ZeroDivisionError):
                    continue

        # 尝试 Mali GPU
        if gpu_usage is None:
            mali_output = _shell(serial, "cat /sys/devices/platform/*.gpu/utilisation 2>/dev/null").strip()
            if mali_output and "No such file" not in mali_output:
                try:
                    gpu_usage = float(mali_output.replace('%', '').strip())
                except ValueError:
                    pass

        return {"gpu": gpu_usage, "time": start_time}

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=15)


# ─────────────────────────── 进程信息 ───────────────────────────

async def harmony_process_info(serial: str, pid: int = 0, package_name: str = "", include_child: bool = False, **kwargs) -> Dict:
    """采集 HarmonyOS 进程的线程数、FD 数等信息。
    include_child=True 时跨进程树聚合线程数与 FD 数。
    """
    def real_func():
        current_time = int(time.time())

        target_pid = pid
        if not target_pid and package_name and not include_child:
            pid_output = _shell(serial, f"pidof {package_name} 2>/dev/null").strip()
            if pid_output:
                parts = pid_output.split()
                target_pid = int(parts[0]) if parts[0].isdigit() else 0

        # include_child：跨进程树聚合线程数 / FD 数。
        if include_child:
            if not target_pid:
                # 无 PID：线程/句柄等进程级信息不可用
                return {"time": current_time, "num_threads": None, "num_handles": None}
            tree_pids = _discover_tree_pids(serial, target_pid)
            num_threads = 0
            num_fds = None
            all_readable = True
            for p in tree_pids:
                status_output = _shell(serial, f"cat /proc/{p}/status 2>/dev/null")
                if not status_output or "No such file" in status_output or "Permission denied" in status_output:
                    all_readable = False
                    break
                thread_match = re.search(r"^Threads:\s*(\d+)", status_output, re.MULTILINE)
                if thread_match:
                    num_threads += int(thread_match.group(1))
                fd_match = re.search(r"^FDSize:\s*(\d+)", status_output, re.MULTILINE)
                if fd_match and int(fd_match.group(1)) > 0:
                    num_fds = (num_fds or 0) + int(fd_match.group(1))
            if not all_readable:
                num_threads = None
                num_fds = None
            return {"time": current_time, "num_threads": num_threads, "num_handles": num_fds}

        # include_child=False：单进程逻辑。
        if not target_pid and package_name:
            pid_output = _shell(serial, f"pidof {package_name} 2>/dev/null").strip()
            if pid_output:
                parts = pid_output.split()
                target_pid = int(parts[0]) if parts[0].isdigit() else 0

        if not target_pid:
            # 无 PID：线程/句柄等进程级信息不可用
            return {"time": current_time, "num_threads": None, "num_handles": None}

        # task/fd 目录在部分商用设备上不可枚举；status 中 Threads/FDSize 可读。
        status_output = _shell(serial, f"cat /proc/{target_pid}/status 2>/dev/null")
        if not status_output or "No such file" in status_output or "Permission denied" in status_output:
            # 进程源缺失（权限不足/进程结束）
            return {"time": current_time, "num_threads": None, "num_handles": None}
        num_threads = 0
        num_fds = None
        thread_match = re.search(r"^Threads:\s*(\d+)", status_output, re.MULTILINE)
        if thread_match:
            num_threads = int(thread_match.group(1))
        else:
            thread_output = _shell(serial, f"ls /proc/{target_pid}/task 2>/dev/null | wc -l").strip()
            num_threads = int(thread_output) if thread_output.isdigit() else 0
        fd_match = re.search(r"^FDSize:\s*(\d+)", status_output, re.MULTILINE)
        if fd_match and int(fd_match.group(1)) > 0:
            num_fds = int(fd_match.group(1))
        else:
            fd_output = _shell(serial, f"ls /proc/{target_pid}/fd 2>/dev/null | wc -l").strip()
            # 受限设备会把 Permission denied 管道到 wc，产生误导性的 0/1。
            if fd_output.isdigit() and int(fd_output) > 1:
                num_fds = int(fd_output)

        return {"time": current_time, "num_threads": num_threads, "num_handles": num_fds}

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=15)


# ─────────────────────────── 磁盘 IO ───────────────────────────

async def harmony_disk_io(serial: str, pid: int = 0, package_name: str = "", include_child: bool = False, **kwargs) -> Dict:
    """采集 HarmonyOS 进程磁盘 I/O（通过 /proc/<pid>/io 两次采样）。
    include_child=True 时跨进程树聚合 read_bytes / write_bytes。
    """
    MB_CONVERSION = 1024 * 1024

    def real_func():
        def parse_io(output):
            result = {}
            for line in output.split('\n'):
                parts = line.strip().split(':')
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip()
                    if val.isdigit():
                        result[key] = int(val)
            return result

        target_pid = pid
        if not target_pid and package_name and not include_child:
            pid_output = _shell(serial, f"pidof {package_name} 2>/dev/null").strip()
            if pid_output:
                parts = pid_output.split()
                target_pid = int(parts[0]) if parts[0].isdigit() else 0

        def read_io(p):
            """读取 /proc/<pid>/io；源缺失（权限/进程结束）返回 None，否则返回解析字典。"""
            out = _shell(serial, f"cat /proc/{p}/io 2>/dev/null")
            if not out or "No such file" in out or "Permission denied" in out:
                return None
            return parse_io(out)

        # include_child：跨进程树聚合 /proc/<pid>/io。
        if include_child:
            if not target_pid:
                # 无 PID：进程级磁盘 IO 不可用
                return {"disk_read_rate": None, "disk_write_rate": None,
                        "disk_read": None, "disk_write": None, "time": int(time.time())}
            tree_pids = _discover_tree_pids(serial, target_pid)
            io1 = {p: read_io(p) for p in tree_pids}
            time.sleep(1)
            io2 = {p: read_io(p) for p in tree_pids}

            if any(v is None for v in io1.values()) or any(v is None for v in io2.values()):
                # 源缺失：无法计算进程级磁盘 IO
                return {"disk_read_rate": None, "disk_write_rate": None,
                        "disk_read": None, "disk_write": None, "time": int(time.time())}

            read_bytes1 = sum(v.get('read_bytes', 0) for v in io1.values())
            write_bytes1 = sum(v.get('write_bytes', 0) for v in io1.values())
            read_bytes2 = sum(v.get('read_bytes', 0) for v in io2.values())
            write_bytes2 = sum(v.get('write_bytes', 0) for v in io2.values())

            disk_read_rate = max(0, (read_bytes2 - read_bytes1) / MB_CONVERSION)
            disk_write_rate = max(0, (write_bytes2 - write_bytes1) / MB_CONVERSION)
            if disk_read_rate < 0.001:
                disk_read_rate = 0
            if disk_write_rate < 0.001:
                disk_write_rate = 0
            return {
                "disk_read_rate": round(disk_read_rate, 4),
                "disk_write_rate": round(disk_write_rate, 4),
                "disk_read": read_bytes2,
                "disk_write": write_bytes2,
                "time": int(time.time())
            }

        # include_child=False：单进程逻辑。
        if not target_pid:
            # 无 PID：进程级磁盘 IO 不可用
            return {"disk_read_rate": None, "disk_write_rate": None,
                    "disk_read": None, "disk_write": None, "time": int(time.time())}

        io1 = read_io(target_pid)
        time.sleep(1)
        io2 = read_io(target_pid)

        if io1 is None or io2 is None:
            # 源缺失：无法计算进程级磁盘 IO
            return {"disk_read_rate": None, "disk_write_rate": None,
                    "disk_read": None, "disk_write": None, "time": int(time.time())}

        read_bytes1 = io1.get('read_bytes', 0)
        write_bytes1 = io1.get('write_bytes', 0)
        read_bytes2 = io2.get('read_bytes', 0)
        write_bytes2 = io2.get('write_bytes', 0)

        disk_read_rate = max(0, (read_bytes2 - read_bytes1) / MB_CONVERSION)
        disk_write_rate = max(0, (write_bytes2 - write_bytes1) / MB_CONVERSION)

        if disk_read_rate < 0.001:
            disk_read_rate = 0
        if disk_write_rate < 0.001:
            disk_write_rate = 0

        res = {
            "disk_read_rate": round(disk_read_rate, 4),
            "disk_write_rate": round(disk_write_rate, 4),
            "disk_read": read_bytes2,
            "disk_write": write_bytes2,
            "time": int(time.time())
        }
        return res

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=20)


# ─────────────────────────── 网络 IO ───────────────────────────

# 缓存必须绑定设备、固定主 PID、include_child 语义及本次 UID 集合。
# 同设备切换测试进程，或子进程树 UID 集合发生变化时，不能与上一采样做差。
_net_io_cache: Dict[tuple, Dict] = {}
_net_io_lock = threading.Lock()


async def harmony_network_io(serial: str, pid: int = 0, package_name: str = "", include_child: bool = False, **kwargs) -> Dict:
    """
    采集 HarmonyOS 网络 I/O。
    按进程树涉及的 UID 集合聚合 /proc/net/xt_qtaguid/stats（UID 去重）。
    绝对禁止回退 /proc/net/dev 整机统计；取不到进程级源时返回 None。
    """
    MB_CONVERSION = 1024 * 1024

    def real_func():
        current_time = int(time.time())

        target_pid = pid
        if not target_pid and package_name and not include_child:
            pid_output = _shell(serial, f"pidof {package_name} 2>/dev/null").strip()
            if pid_output:
                parts = pid_output.split()
                target_pid = int(parts[0]) if parts[0].isdigit() else 0

        if not target_pid:
            # 无 PID：进程级网络 IO 不可用
            return {"net_sent_rate": None, "net_recv_rate": None,
                    "net_sent": None, "net_recv": None, "time": current_time}

        # include_child 时跨进程树收集 UID；否则仅主进程 UID。UID 去重。
        tree_pids = _discover_tree_pids(serial, target_pid) if include_child else [target_pid]
        uid_set: set = set()
        for p in tree_pids:
            uid_output = _shell(serial, f"cat /proc/{p}/status 2>/dev/null | grep Uid").strip()
            if not uid_output or "No such file" in uid_output or "Permission denied" in uid_output:
                # 进程源缺失，无法获取 UID
                uid_set = None
                break
            match = re.search(r'Uid:\s+(\d+)', uid_output)
            if match:
                uid_set.add(int(match.group(1)))

        if not uid_set:
            # 无法获取进程 UID（权限不足/进程源缺失），无进程级数据源
            return {"net_sent_rate": None, "net_recv_rate": None,
                    "net_sent": None, "net_recv": None, "time": current_time}

        # 仅使用进程级源 /proc/net/xt_qtaguid/stats，按 UID 聚合；
        # 源不可用（缺失/权限不足）返回 None，源存在但无流量（差值为 0）保留真实 0。
        # 绝对禁止回退整机 /proc/net/dev。
        output = _shell(serial, "cat /proc/net/xt_qtaguid/stats 2>/dev/null").strip()
        if not output or "No such file" in output or "Permission denied" in output:
            return {"net_sent_rate": None, "net_recv_rate": None,
                    "net_sent": None, "net_recv": None, "time": current_time}

        rx_now = tx_now = 0
        for line in output.split('\n')[1:]:
            parts = line.strip().split()
            if len(parts) >= 8:
                try:
                    if int(parts[3]) in uid_set:
                        rx_now += int(parts[5])
                        tx_now += int(parts[7])
                except (ValueError, IndexError):
                    continue

        # 速率缓存与固定主 PID 和实际 UID 集合绑定。子树变化时首帧速率归零，
        # 避免拿不同统计范围的累计字节数相减。
        cache_key = (serial, target_pid, include_child, frozenset(uid_set))
        with _net_io_lock:
            cache = _net_io_cache.get(cache_key)
            if cache:
                dt = current_time - cache["time"]
                if dt > 0:
                    recv_rate = max(0, (rx_now - cache["net_in"]) / MB_CONVERSION / dt)
                    sent_rate = max(0, (tx_now - cache["net_out"]) / MB_CONVERSION / dt)
                else:
                    recv_rate = sent_rate = 0
            else:
                recv_rate = sent_rate = 0

            _net_io_cache[cache_key] = {
                "time": current_time,
                "net_in": rx_now,
                "net_out": tx_now,
            }

        return {
            "net_sent_rate": round(sent_rate, 4),
            "net_recv_rate": round(recv_rate, 4),
            "net_sent": tx_now,
            "net_recv": rx_now,
            "time": current_time
        }

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=20)


# ─────────────────────────── 电池信息 ───────────────────────────

async def harmony_battery(serial: str, **kwargs) -> Dict:
    """
    采集 HarmonyOS 设备电池信息
    通过 hidumper -s BatteryService 获取
    """
    def real_func():
        battery_info = {"time": int(time.time())}

        try:
            output = _shell(serial, "hidumper -s BatteryService -a -i 2>/dev/null", timeout=10)
            if not output:
                output = _shell(serial, "hidumper -s BatteryService 2>/dev/null", timeout=10)

            if output:
                # 电量
                match = re.search(r'capacity[:\s=]+(\d+)', output, re.IGNORECASE)
                if match:
                    battery_info['battery_level'] = int(match.group(1))
                # 温度（单位 0.1°C）
                match = re.search(r'temperature[:\s=]+(-?\d+)', output, re.IGNORECASE)
                if match:
                    battery_info['battery_temperature'] = round(int(match.group(1)) / 10.0, 1)
                # 电流（μA → mA）
                match = re.search(r'current[:\s=]+(-?\d+)', output, re.IGNORECASE)
                if match:
                    battery_info['battery_current'] = round(int(match.group(1)) / 1000.0, 2)
        except Exception as e:
            logger.warning(f"hidumper BatteryService 失败: {e}")

        battery_info.setdefault('battery_level', 0)
        battery_info.setdefault('battery_temperature', 0)
        battery_info.setdefault('battery_current', 0)
        return battery_info

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=15)


# ─────────────────────────── 截图 ───────────────────────────

async def harmony_screenshot(serial: str, save_dir: str = None, **kwargs):
    """HarmonyOS 设备截图（hdc shell snapshot_display）"""
    def real_func():
        if not HDC_PATH:
            return None

        timestamp = int(time.time())
        # 当前 HarmonyOS snapshot_display 仅接受 jpeg 后缀。
        remote_path = f"/data/local/tmp/hm_shot_{timestamp}.jpeg"
        _shell(serial, f"snapshot_display -f {remote_path} 2>/dev/null")
        time.sleep(0.5)

        if save_dir:
            screenshot_dir = Path(save_dir) / "screenshot"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            local_path = str(screenshot_dir / f"{timestamp}.jpeg")
        else:
            local_path = f"/tmp/hm_screenshot_{timestamp}.jpeg"

        # 拉取截图到本地；无论成功、失败或取消，都尽力清理设备临时文件。
        try:
            subprocess.run(
                [HDC_PATH, "-t", serial, "file", "recv", remote_path, local_path],
                capture_output=True, timeout=15
            )
            if os.path.isfile(local_path) and os.path.getsize(local_path) > 0:
                if not save_dir:
                    with open(local_path, "rb") as f:
                        data = f.read()
                    os.remove(local_path)
                    return data
                return True
        except Exception as e:
            logger.error(f"HarmonyOS 截图失败: {e}")
        finally:
            _shell(serial, f"rm -f {remote_path} 2>/dev/null")
        return None

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=20)


# ─────────────────────────── 性能采集入口 ───────────────────────────

async def harmony_perf(serial: str, package_name: str, pid: int, save_dir: str, include_child: bool = False):
    """
    HarmonyOS 性能采集入口，与 Android/iOS 端保持一致的 Monitor 结构。

    支持的指标：
    - CPU 使用率（/proc/stat 两次采样）
    - 内存使用（hidumper --mem 或 /proc/<pid>/status）
    - FPS（无可信进程级数据源时返回 None）
    - GPU（/sys 节点）
    - 网络 IO（/proc/net/dev 缓存差值）
    - 磁盘 IO（/proc/<pid>/io 两次采样）
    - 电池（hidumper -s BatteryService）
    - 截图（snapshot_display + hdc file recv）
    - 进程信息（线程数、FD 数）
    """
    # 如果没有 pid，尝试通过包名获取
    if not pid and package_name:
        try:
            pid_output = _shell(serial, f"pidof {package_name} 2>/dev/null").strip()
            if pid_output:
                parts = pid_output.split()
                pid = int(parts[0]) if parts[0].isdigit() else 0
        except Exception:
            pid = 0

    logger.info(f"HarmonyOS 性能采集: serial={serial}, package={package_name}, pid={pid}")

    monitors = {
        "cpu": Monitor(harmony_cpu,
                       serial=serial, pid=pid, package_name=package_name,
                       include_child=include_child,
                       monitor_name="cpu",
                       key_value=["time", "cpu_usage(%)", "cpu_usage_all(%)", "cpu_core_num(个)"],
                       save_dir=save_dir),
        "memory": Monitor(harmony_memory,
                          serial=serial, pid=pid, package_name=package_name,
                          include_child=include_child,
                          monitor_name="memory",
                          key_value=["time", "process_memory_usage(M)"],
                          save_dir=save_dir),
        "process_info": Monitor(harmony_process_info,
                                serial=serial, pid=pid, package_name=package_name,
                                include_child=include_child,
                                monitor_name="process_info",
                                key_value=["time", "num_threads(个)", "num_handles(个)"],
                                save_dir=save_dir),
        "fps": Monitor(harmony_fps,
                       serial=serial, pid=pid, package_name=package_name,
                       monitor_name="fps",
                       key_value=["time", "fps(帧)", "frames"],
                       save_dir=save_dir),
        "gpu": Monitor(harmony_gpu,
                       serial=serial,
                       monitor_name="gpu",
                       key_value=["time", "gpu(%)"],
                       save_dir=save_dir),
        "disk_io": Monitor(harmony_disk_io,
                           serial=serial, pid=pid, package_name=package_name,
                           include_child=include_child,
                           monitor_name="disk_io",
                           key_value=["time", "disk_read_rate(MB/s)", "disk_write_rate(MB/s)",
                                      "disk_read(字节)", "disk_write(字节)"],
                           save_dir=save_dir),
        "network_io": Monitor(harmony_network_io,
                              serial=serial, pid=pid, package_name=package_name,
                              include_child=include_child,
                              monitor_name="network_io",
                              key_value=["time", "net_sent_rate(MB/s)", "net_recv_rate(MB/s)",
                                         "net_sent(字节)", "net_recv(字节)"],
                              save_dir=save_dir),
        "battery": Monitor(harmony_battery,
                           serial=serial,
                           monitor_name="battery",
                           key_value=["time", "battery_level(%)", "battery_temperature(℃)",
                                      "battery_current(mA)"],
                           save_dir=save_dir),
        "screenshot": Monitor(harmony_screenshot,
                              serial=serial,
                              save_dir=save_dir, is_out=False)
    }
    run_monitors = [monitor.run() for name, monitor in monitors.items()]
    await asyncio.gather(*run_monitors)
