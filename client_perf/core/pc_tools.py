import asyncio
import json
import platform
import subprocess
import threading
import time
import traceback
from io import BytesIO
import psutil
import pynvml
from pathlib import Path
from client_perf.log import log as logger
from client_perf.core.monitor import Monitor

MB_CONVERSION = 1024 * 1024


def _is_admin() -> bool:
    if platform.system() != "Windows":
        return True
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False

SUPPORT_GPU = True
try:
    pynvml.nvmlInit()
except Exception:
    logger.info("本设备gpu获取不适配")
    SUPPORT_GPU = False

try:
    from PIL import ImageGrab
    SCREENSHOT_AVAILABLE = True
except ImportError:
    SCREENSHOT_AVAILABLE = False
    logger.info("Pillow ImageGrab 不可用（Linux 无桌面环境），截图功能已禁用")


def print_json(msg):
    logger.info(json.dumps(msg))


class WinFps(object):
    frame_que = list()
    single_instance = None
    fps_process = None
    _admin_warned = False
    _start_lock = threading.Lock()

    def __init__(self, pid):
        self.pid = pid

    def __new__(cls, *args, **kwargs):
        if not cls.single_instance:
            cls.single_instance = super().__new__(cls)
        return cls.single_instance

    def fps(self):
        if WinFps.fps_process is None:
            with WinFps._start_lock:
                if WinFps.fps_process is None:
                    threading.Thread(
                        target=self.start_fps_collect,
                        args=(self.pid,),
                        daemon=True,
                    ).start()
        if self.check_queue_head_frames_complete():
            return self.pop_complete_fps()

    @staticmethod
    def check_queue_head_frames_complete():
        if not WinFps.frame_que:
            return False
        head_time = int(WinFps.frame_que[0])
        end_time = int(WinFps.frame_que[-1])
        if head_time == end_time:
            return False
        return True

    @staticmethod
    def pop_complete_fps():
        head_time = int(WinFps.frame_que[0])
        complete_fps = []
        while int(WinFps.frame_que[0]) == head_time:
            complete_fps.append(WinFps.frame_que.pop(0))
        return complete_fps

    def start_fps_collect(self, pid):
        if platform.system() != "Windows":
            return

        if not _is_admin():
            if not WinFps._admin_warned:
                WinFps._admin_warned = True
                logger.error(
                    "PresentMon 需要管理员权限才能采集 FPS。"
                    "请以管理员身份运行 client-perf，或启动时不要使用 --no-elevate 参数。"
                )
            return

        start_fps_collect_time = int(time.time())
        PresentMon = Path(__file__).parent.parent.joinpath(
            "tool",
            f"PresentMon-1.8.0-{'x64' if platform.machine() == 'AMD64' else 'x86'}.exe",
        )
        if not PresentMon.exists():
            logger.error(f"PresentMon.exe 不存在: {PresentMon}")
            return

        process = None
        try:
            process = subprocess.Popen(
                [
                    str(PresentMon),
                    "-process_id",
                    str(pid),
                    "-output_stdout",
                    "-stop_existing_session",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            WinFps.fps_process = process
            if process.stdout is not None:
                process.stdout.readline()
            while process.poll() is None:
                if process.stdout is None:
                    break
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    line = line.decode("utf-8")
                    line_list = line.split(",")
                    WinFps.frame_que.append(
                        start_fps_collect_time + round(float(line_list[7]), 7)
                    )
                except Exception:
                    logger.error(traceback.format_exc())
        except Exception:
            logger.error(traceback.format_exc())
        finally:
            current = WinFps.fps_process
            if current is not None:
                try:
                    if current.poll() is None:
                        current.kill()
                except Exception:
                    pass
            WinFps.fps_process = None


async def sys_info():
    def real_func():
        current_platform = platform.system()
        computer_name = platform.node()
        res = {"platform": current_platform, "computer_name": computer_name, "time": time.time(),
               "cpu_cores": psutil.cpu_count(), "ram": "{0}G".format(int(psutil.virtual_memory().total / 1024 ** 3)),
               "rom": "{0}G".format(int(psutil.disk_usage('/').total / 1024 ** 3))}
        print_json(res)
        return res

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=10)


async def pids():
    def real_func():
        process_list = []
        for proc in psutil.process_iter(attrs=['name', 'pid', 'cmdline', 'username']):
            try:
                if proc.is_running():
                    process_list.append(
                        {"name": proc.info['name'], "pid": proc.info['pid'], "cmd": proc.info['cmdline'],
                         "username": proc.username()})
            except Exception as e:
                pass
        process_list.sort(key=lambda x: x['name'])
        # print_json(process_list)
        return process_list

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=10)


def get_visible_top_level_windows():
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    EnumWindows = user32.EnumWindows
    IsWindowVisible = user32.IsWindowVisible
    GetWindowTextLengthW = user32.GetWindowTextLengthW
    GetWindowTextW = user32.GetWindowTextW
    GetWindowThreadProcessId = user32.GetWindowThreadProcessId

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    results = []

    @EnumWindowsProc
    def enum_proc(hwnd, lParam):
        if IsWindowVisible(hwnd):
            length = GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                if title:
                    pid = wintypes.DWORD()
                    GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    results.append(pid.value)
        return True

    EnumWindows(enum_proc, 0)
    return results


async def process_tree():
    def real_func():
        process_list = []
        if platform.system() == "Windows":
            appliction_pids = get_visible_top_level_windows()
        else:
            appliction_pids = [0, 1]
        for proc in psutil.process_iter(attrs=['name', 'pid', 'cmdline', 'username', 'ppid']):
            try:
                # 检查进程是否正在运行且父进程 ID 为 1
                if proc.is_running() and (
                        proc.pid if platform.system() == "Windows" else proc.ppid()) in appliction_pids:
                    process_info = {
                        "name": proc.info['name'],
                        "ppid": proc.info['ppid'],
                        "pid": proc.info['pid'],
                        "cmd": proc.info['cmdline'],
                        "username": proc.username(),
                        "child_p": []
                    }
                    try:
                        # 获取子进程信息
                        children = proc.children(recursive=True)
                        for child in children:
                            try:
                                child_info = {
                                    "name": child.name(),
                                    "ppid": child.ppid(),
                                    "pid": child.pid,
                                    "cmd": child.cmdline(),
                                    "username": child.username()
                                }
                                process_info["child_p"].append(child_info)
                            except Exception:
                                # 处理子进程不存在的情况
                                pass
                    except psutil.NoSuchProcess:
                        # 处理父进程不存在的情况
                        logger.error(f"父进程 {proc.pid} 已不存在")
                    process_list.append(process_info)
            except Exception as e:
                logger.error(e)
        process_list.sort(key=lambda x: -len(x['child_p']))
        # print_json(process_list)
        return process_list

    return await asyncio.wait_for(asyncio.to_thread(real_func), timeout=10)


# 新增全局变量，用于缓存窗口句柄和时间戳 (window, timestamp)
window_cache = {}
# 设置缓存过期时间（秒）
WINDOW_CACHE_EXPIRE_TIME = 15


async def screenshot(pid, save_dir, include_child=False):
    def real_func(pid, save_dir):
        if not SCREENSHOT_AVAILABLE:
            return None
        global window_cache
        if pid:
            window = (window_cache.get(pid)[0] if window_cache.get(pid)[-1] + WINDOW_CACHE_EXPIRE_TIME > time.time() else None) if window_cache.get(pid) else None
            if platform.system() == "Windows" and window is None:
                import ctypes
                import pygetwindow as gw
                def get_pid(hwnd):
                    pid = ctypes.wintypes.DWORD()
                    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    return pid.value

                def get_window_by_pid(pids):
                    for window in gw.getAllWindows():
                        if get_pid(window._hWnd) in pids:
                            return window
                    return None

                pids = [pid]
                if include_child:
                    process = psutil.Process(int(pid))
                    p_chs = process.children(recursive=True)
                    if p_chs:
                        sub_pid = [sub_p.pid for sub_p in p_chs]
                        pids.extend(sub_pid)
                window = get_window_by_pid(pids)
                window_cache[pid] = (window, time.time())

            if window:
                screenshot = ImageGrab.grab(
                    bbox=(window.left, window.top, window.left + window.width, window.top + window.height),
                    all_screens=True)
            else:
                screenshot = ImageGrab.grab(all_screens=True)
            if save_dir:
                dir_instance = Path(save_dir)
                screenshot_dir = dir_instance.joinpath("screenshot")
                screenshot_dir.mkdir(exist_ok=True)
                screenshot.save(screenshot_dir.joinpath(str(int(time.time() + 0.5)) + ".png"), format="PNG")
            else:
                output_buffer = BytesIO()
                screenshot.save(output_buffer, format='PNG')
                output_buffer.seek(0)  # 重置缓冲区指针
                image_data = output_buffer.getvalue()
                return image_data

    return await asyncio.wait_for(asyncio.to_thread(real_func, pid, save_dir), timeout=10)


async def cpu(pid, include_child=False):
    start_time = int(time.time())
    try:
        process = psutil.Process(int(pid))
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        # 无效 PID / 无权限 / 无数据源：相关数值字段为 None，所有字段始终存在
        return {"cpu_usage": None, "cpu_usage_all": None,
                "cpu_core_num": psutil.cpu_count(), "time": start_time}

    get_main_cpu = asyncio.to_thread(process.cpu_percent, interval=1)
    tasks = [get_main_cpu]
    if include_child:
        children = process.children(recursive=True)
        if children:
            tasks.extend([asyncio.to_thread(child.cpu_percent, interval=1)
                          for child in children])
    all_cpu_values = await asyncio.gather(*tasks, return_exceptions=True)
    total_cpu_usage = sum(v for v in all_cpu_values if not isinstance(v, Exception))
    cpu_count = psutil.cpu_count()
    res = {
        "cpu_usage": total_cpu_usage / cpu_count,
        "cpu_usage_all": total_cpu_usage,
        "cpu_core_num": cpu_count,
        "time": start_time
    }
    print_json(res)
    return res


async def memory(pid, include_child=False):
    start_time = int(time.time())
    try:
        process = psutil.Process(int(pid))
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        # 无效 PID / 无权限 / 无数据源：相关数值字段为 None，所有字段始终存在
        return {"process_memory_usage": None, "time": start_time}

    get_main_mem = asyncio.to_thread(lambda: process.memory_info().rss / (1024 ** 2))
    tasks = [get_main_mem]
    if include_child:
        p_chs = process.children(recursive=True)
        if p_chs:
            tasks.extend([asyncio.to_thread(lambda p=sub_p: p.memory_info().rss / (1024 ** 2))
                          for sub_p in p_chs])
    all_mem_values = await asyncio.gather(*tasks, return_exceptions=True)
    total_memory = sum(v for v in all_mem_values if not isinstance(v, Exception))
    res = {"process_memory_usage": total_memory, "time": start_time}
    print_json(res)
    return res


async def fps(pid, include_child=False):
    pid = int(pid)
    if platform.system() != "Windows":
        # 非 Windows 无 FPS 数据源：数值字段为 None，frames 用空列表，所有字段始终存在
        res = {"type": "fps", "fps": None, "frames": [], "time": int(time.time())}
        print_json(res)
        return res
    frames = WinFps(pid).fps()
    if not frames:
        # 暂无 FPS 数据：frames 用空列表，fps 为 None，所有字段始终存在
        res = {"type": "fps", "fps": None, "frames": [], "time": int(time.time())}
        print_json(res)
        return res
    res = {"type": "fps", "fps": len(frames), "frames": frames, "time": int(frames[0])}
    print_json(res)
    return res


async def gpu(pid, include_child=False):
    pid = int(pid)

    def real_func(pid):
        start_time = int(time.time())
        try:
            process = psutil.Process(int(pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            # 无效 PID / 无权限 / 无数据源：数值字段为 None，所有字段始终存在
            return {"gpu": None, "time": start_time}

        pids = [pid]
        if include_child:
            try:
                p_chs = process.children(recursive=True)
                if p_chs:
                    pids.extend([sub_p.pid for sub_p in p_chs])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        if not SUPPORT_GPU:
            # 平台不支持 GPU 采集：数值字段为 None，所有字段始终存在
            return {"gpu": None, "time": start_time}

        try:
            device_count = pynvml.nvmlDeviceGetCount()
            gpu_utilization_percentage = None
            for i in range(device_count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                processes = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)
                if not any(proc.pid in pids for proc in processes):
                    continue
                utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
                current_gpu = utilization.gpu
                if (
                    gpu_utilization_percentage is None
                    or current_gpu > gpu_utilization_percentage
                ):
                    gpu_utilization_percentage = current_gpu
            return {"gpu": gpu_utilization_percentage, "time": start_time}
        except Exception as e:
            logger.error(f"获取GPU数据失败: {str(e)}")
            # 采集失败：数值字段为 None，所有字段始终存在
            return {"gpu": None, "time": start_time}

    return await asyncio.wait_for(asyncio.to_thread(real_func, pid), timeout=10)


async def process_info(pid, include_child=False):
    start_time = int(time.time())
    try:
        process = psutil.Process(int(pid))
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        # 无效 PID / 无权限 / 无数据源：相关数值字段为 None，所有字段始终存在
        return {"time": start_time, "num_handles": None, "num_threads": None}

    support_handles = hasattr(process, "num_handles")
    num_handles = 0 if support_handles else None
    num_threads = 0

    handles_task = [asyncio.to_thread(process.num_handles)] if support_handles else []
    threads_task = [asyncio.to_thread(process.num_threads)]
    if include_child:
        children = process.children(recursive=True)
        if children:
            if support_handles:
                handles_task.extend([asyncio.to_thread(child.num_handles) for child in children])
            threads_task.extend([asyncio.to_thread(child.num_threads) for child in children])

    if handles_task:
        all_num_handles_values = await asyncio.gather(*handles_task, return_exceptions=True)
        num_handles = sum(v for v in all_num_handles_values if not isinstance(v, Exception))
    all_num_threads_values = await asyncio.gather(*threads_task, return_exceptions=True)
    num_threads = sum(v for v in all_num_threads_values if not isinstance(v, Exception))

    # 所有字段始终存在：支持且采集到 0 保留 0；不支持则保持 None
    res = {"time": start_time, "num_handles": num_handles, "num_threads": num_threads}
    return res


async def disk_io(pid, include_child=False):
    """监控进程的磁盘I/O指标（聚合主进程 + 递归子进程）"""
    start_time = int(time.time())

    def _none_result():
        # 采集失败 / 无数据源 / 无效 PID：相关数值字段为 None，所有字段始终存在
        return {"disk_read_rate": None, "disk_write_rate": None,
                "disk_read": None, "disk_write": None, "time": start_time}

    try:
        process = psutil.Process(int(pid))
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        # 无效 PID / 无权限 / 无数据源：相关数值字段为 None，所有字段始终存在
        return _none_result()

    # 收集主进程 + 递归子进程；任一进程退出/无权限时忽略，不影响其余聚合
    targets = [process]
    if include_child:
        try:
            children = process.children(recursive=True)
            targets.extend(children)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    def read_snapshot(procs):
        """一次性快照所有目标进程的累计读写字节数；进程异常则跳过。"""
        snap = {}
        for p in procs:
            try:
                io = p.io_counters()
                snap[p.pid] = (io.read_bytes, io.write_bytes)
            except Exception:
                # 进程可能在两次快照间退出，或无权访问，跳过该进程
                pass
        return snap

    try:
        prev = await asyncio.to_thread(read_snapshot, targets)
        await asyncio.sleep(1)
        curr = await asyncio.to_thread(read_snapshot, targets)

        # 所有目标进程都无法提供 io_counters（如平台不支持磁盘 I/O）：视为无数据源
        if not prev and not curr:
            return _none_result()

        prev_read = sum(v[0] for v in prev.values())
        prev_write = sum(v[1] for v in prev.values())
        curr_read = sum(v[0] for v in curr.values())
        curr_write = sum(v[1] for v in curr.values())

        disk_read_rate = max(0, (curr_read - prev_read) / MB_CONVERSION)
        disk_write_rate = max(0, (curr_write - prev_write) / MB_CONVERSION)

        # 忽略小于约1KB/s 的读写操作（真实采集到 0 仍保留 0）
        if disk_read_rate < 0.001:
            disk_read_rate = 0
        if disk_write_rate < 0.001:
            disk_write_rate = 0

        res = {
            "disk_read_rate": round(disk_read_rate, 2),  # MB/s
            "disk_write_rate": round(disk_write_rate, 2),  # MB/s
            "disk_read": curr_read,  # 总读取字节数（主+递归子进程累计）
            "disk_write": curr_write,  # 总写入字节数（主+递归子进程累计）
            "time": start_time
        }

        logger.info(json.dumps(res))
        return res
    except (psutil.AccessDenied, AttributeError) as e:
        logger.error(f"获取磁盘I/O数据失败: {str(e)}")
        return _none_result()


async def network_io(pid, include_child=False):
    """监控进程的网络I/O指标。

    注意：psutil.net_io_counters() 仅提供整机（机器级）网络统计，
    并非按 PID 拆分，不存在可靠的“按 PID 字节源”可聚合主进程与子进程。
    为避免用整机值冒充进程数据，且遵循“无数据源相关字段为 None”的规范，
    这里统一返回 None，所有字段始终存在。
    如需真正的按 PID 网络统计需借助平台专用手段（如 Linux /proc、ETW 等）。
    """
    start_time = int(time.time())
    return {
        "net_sent_rate": None,  # MB/s
        "net_recv_rate": None,  # MB/s
        "net_sent": None,  # 总发送字节数
        "net_recv": None,  # 总接收字节数
        "time": start_time
    }


async def perf(pid, save_dir, include_child):
    monitors = {
        "cpu": Monitor(cpu,
                       pid=pid,
                       key_value=["time", "cpu_usage(%)", "cpu_usage_all(%)", "cpu_core_num(个)"],
                       save_dir=save_dir, include_child=include_child),
        "memory": Monitor(memory,
                          pid=pid,
                          key_value=["time", "process_memory_usage(M)"],
                          save_dir=save_dir, include_child=include_child),
        "process_info": Monitor(process_info,
                                pid=pid,
                                key_value=["time", "num_threads(个)", "num_handles(个)"],
                                save_dir=save_dir, include_child=include_child),
        "fps": Monitor(fps,
                       pid=pid,
                       key_value=["time", "fps(帧)", "frames"],
                       save_dir=save_dir, include_child=include_child),
        "gpu": Monitor(gpu,
                       pid=pid,
                       key_value=["time", "gpu(%)"],
                       save_dir=save_dir, include_child=include_child),
        "disk_io": Monitor(disk_io,
                          pid=pid,
                          key_value=["time", "disk_read_rate(MB/s)", "disk_write_rate(MB/s)", "disk_read(字节)", "disk_write(字节)"],
                          save_dir=save_dir, include_child=include_child),
        "network_io": Monitor(network_io,
                             pid=pid,
                             key_value=["time", "net_sent_rate(MB/s)", "net_recv_rate(MB/s)", "net_sent(字节)", "net_recv(字节)"],
                             save_dir=save_dir, include_child=include_child),
        "screenshot": Monitor(screenshot,
                              pid=pid,
                              save_dir=save_dir, is_out=False, include_child=include_child)
    }
    run_monitors = [monitor.run() for name, monitor in monitors.items()]
    await asyncio.gather(*run_monitors)
