# client_perf 功能问题修复方案

> 仅处理会影响实际功能、采集结果或任务可靠性的问题。
>
> 已排除：鉴权/权限、安全泄漏、0.0.0.0、本地 SQLite、企业化架构、Windows ARM64，以及纯代码风格问题。
>
> 当前任务模型：**一个 TaskHandle 对应一个独立子进程**。因此 WinFps 的 class-level 状态不会跨 Task 共享，不能把 Singleton 本身视为跨任务串数据 Bug。

## 一、最终修复清单

| 优先级 | 问题 | 影响 | 主要文件 |
|---|---|---|---|
| P0 | DataCollect 根据第一条记录判断数值字段 | 首条记录为 None 时，后续真实数值可能完全不参与统计 | `client_perf/util.py` |
| P1 | TaskHandle 异常仍可能被标记为 STOPPED | 采集失败在 UI/接口上表现为正常完成 | `task_handle.py`、`db.py` |
| P1 | PresentMon 异常退出后 FPS 不恢复 | FPS 采集可能从异常点开始一直为空 | `core/pc_tools.py` |
| P1 | 多 GPU 使用率覆盖 | 多 GPU 环境结果可能不准确 | `core/pc_tools.py` |
| P1 | iOS Simulator CLI 入口不完整 | 已实现能力无法从部分 CLI 入口使用 | `cli.py` |
| P1 | 任务创建/启动/删除状态竞态 | 极端情况下 DB 状态与实际 TaskHandle 状态不一致 | `routers/tasks.py`、`db.py` |

---

## 二、问题 1：DataCollect 统计字段识别错误

### 当前问题

当前逻辑根据第一条完整记录决定数值字段：

```python
numeric_keys = [
    k for k in full_rows[0]
    if k != "time" and isinstance(full_rows[0][k], (int, float))
]
```

例如：

```python
[
    {"time": 1000, "cpu_usage": None},
    {"time": 1001, "cpu_usage": 30},
    {"time": 1002, "cpu_usage": 40},
]
```

第一行是 `None`，因此 `cpu_usage` 不会进入 `numeric_keys`，最终可能得到空的 `avg_value/max_value`。

### 修复

扫描所有行，只要字段出现过数值就纳入统计：

```python
numeric_keys: set[str] = set()

for row in full_rows:
    for key, value in row.items():
        if key == "time":
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric_keys.add(key)

max_val: dict[str, float] = {}
sum_val: dict[str, float] = {k: 0.0 for k in numeric_keys}
cnt_val: dict[str, int] = {k: 0 for k in numeric_keys}

for row in full_rows:
    for key in numeric_keys:
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            max_val[key] = max(max_val.get(key, value), value)
            sum_val[key] += value
            cnt_val[key] += 1

data["max_value"] = {
    key: round(max_val[key], 4)
    for key in numeric_keys
    if key in max_val
}

data["avg_value"] = {
    key: round(sum_val[key] / cnt_val[key], 4)
    for key in numeric_keys
    if cnt_val[key] > 0
}
```

验收：

```python
avg_value["cpu_usage"] == 35
max_value["cpu_usage"] == 40
```

---

## 三、问题 2：采集异常被标记为正常结束

### 当前问题

当前 `TaskHandle.run()` 是：

```python
try:
    ...
except Exception:
    logger.error(traceback.format_exc())
finally:
    asyncio.run(TaskCollection.stop_task(self.task_id))
```

因此异常发生后最终仍然执行 `stop_task()`。

同时当前 `fail_task()` 实际也是把状态设成 `2`，没有真正区分失败。

### 修复方案

定义状态：

```python
TASK_CREATED = 0
TASK_RUNNING = 1
TASK_STOPPED = 2
TASK_FAILED = 3
```

修改 `fail_task()`：

```python
@classmethod
async def fail_task(cls, task_id: int) -> dict[str, Any]:
    async with _Session() as s, s.begin():
        task = await s.get(TaskModel, task_id)
        if not task:
            raise RuntimeError(f"任务 {task_id} 不存在")

        task.status = TASK_FAILED
        task.end_time = _now()

    return _model_to_dict(task)
```

修改 `TaskHandle.run()`：

```python
def run(self) -> None:
    logger.info(
        f"[TaskHandle] start task_id={self.task_id} "
        f"device_type={self.device_type} device_id={self.device_id} "
        f"package={self.package_name}"
    )

    asyncio.run(TaskCollection.set_task_running(self.task_id, self.pid))

    try:
        if self.device_type == "android":
            self._run_android()
        elif self.device_type == "ios":
            self._run_ios()
        elif self.device_type == "ios_simulator":
            self._run_ios_simulator()
        elif self.device_type == "harmony":
            self._run_harmony()
        else:
            self._run_pc()
    except Exception:
        logger.error(traceback.format_exc())
        try:
            asyncio.run(TaskCollection.fail_task(self.task_id))
        except Exception:
            logger.error(
                "更新失败任务状态失败:\n%s",
                traceback.format_exc(),
            )
    else:
        try:
            asyncio.run(TaskCollection.stop_task(self.task_id))
        except Exception:
            logger.error(
                "更新已结束任务状态失败:\n%s",
                traceback.format_exc(),
            )
```

最终语义：

```text
正常结束 -> STOPPED
采集异常 -> FAILED
```

---

## 四、问题 3：PresentMon 异常退出后 FPS 不恢复

### 当前问题

PresentMon 启动后：

```python
WinFps.fps_process = res_terminate
```

如果 PresentMon 中途退出，`fps_process` 仍然保存着已经结束的 `Popen` 对象。

下一次：

```python
if not WinFps.fps_process:
```

仍然不会重新启动。

### 修复

给 FPS collector 增加清理逻辑：

```python
def start_fps_collect(self, pid):
    if platform.system() != "Windows":
        return

    try:
        # 管理员权限、PresentMon 路径检查略...

        res_terminate = subprocess.Popen(
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

        WinFps.fps_process = res_terminate

        res_terminate.stdout.readline()

        while res_terminate.poll() is None:
            line = res_terminate.stdout.readline()
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
        process = WinFps.fps_process

        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
            except Exception:
                pass

        WinFps.fps_process = None
```

### 同时防止启动竞态

当前：

```python
if not WinFps.fps_process:
    threading.Thread(...).start()
```

建议：

```python
class WinFps:
    frame_que = []
    single_instance = None
    fps_process = None
    _start_lock = threading.Lock()
```

然后：

```python
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
```

这里不需要 IPC、Manager 或 multiprocessing.Queue，因为一个 Task 本身就是独立子进程。

---

## 五、问题 4：多 GPU 使用率覆盖

### 当前问题

当前代码遍历 GPU：

```python
for i in range(device_count):
    ...
    if proc.pid in pids:
        gpu_utilization_percentage = gpu_Utilization.gpu
```

如果：

```text
GPU0 = 30%
GPU1 = 50%
```

最终只剩：

```text
gpu = 50
```

前面的 GPU 被覆盖。

### 修复

当前 API 只有一个 `gpu` 字段，因此建议采用：

> 目标进程所在 GPU 中的最大 utilization。

```python
gpu_utilization_percentage = None

for i in range(device_count):
    handle = pynvml.nvmlDeviceGetHandleByIndex(i)
    processes = pynvml.nvmlDeviceGetComputeRunningProcesses(handle)

    if any(proc.pid in pids for proc in processes):
        utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
        current_gpu = utilization.gpu

        if (
            gpu_utilization_percentage is None
            or current_gpu > gpu_utilization_percentage
        ):
            gpu_utilization_percentage = current_gpu

return {
    "gpu": gpu_utilization_percentage,
    "time": start_time,
}
```

不建议直接求和，例如 80% + 70% = 150% 对当前单值 `gpu` 字段没有明确意义。

---

## 六、问题 5：iOS Simulator CLI 入口不完整

### 当前问题

内部已经支持：

```text
ios_simulator
```

TaskHandle 也已经有：

```python
elif self.device_type == "ios_simulator":
    self._run_ios_simulator()
```

但 CLI 的 `system-info` 当前只有：

```python
choices=("pc", "android", "ios", "harmony")
```

### 修复

修改为：

```python
system_info.add_argument(
    "--device-type",
    default="pc",
    choices=(
        "pc",
        "android",
        "ios",
        "ios_simulator",
        "harmony",
    ),
)
```

验收：

```bash
client-perf system-info --device-type ios_simulator
```

参数解析必须成功。

### 注意

`apps` 是否增加 `ios_simulator` 不要直接强行修改，要以 `DeviceManager.get_device_apps_async()` 是否已经完整支持 Simulator 为准。本次只修已经明确存在但 CLI 没暴露的 `system-info` 能力。

---

## 七、问题 6：任务创建/启动/删除存在状态竞态

### 当前流程

```text
create_task()
    ↓
status = 0
    ↓
创建 TaskHandle
    ↓
handle.start()
    ↓
TaskHandle.run()
    ↓
status = 1
```

删除逻辑只禁止：

```python
if task.status == 1:
    raise RuntimeError("任务运行中，不能删除")
```

因此极端情况下：

```text
T1 create_task()
   status = 0

T2 delete_task()
   删除成功

T3 handle.start()
   TaskHandle 启动
```

之后子进程再调用：

```python
set_task_running(task_id)
```

可能发现 Task 已经不存在。

### 推荐方案

最小改动下，将：

```text
status = 1
```

定义为：

```text
STARTING / RUNNING
```

这样任务创建后、真正启动子进程前，就进入不可删除状态。

增加：

```python
@classmethod
async def mark_task_starting(cls, task_id: int) -> dict[str, Any]:
    async with _Session() as s, s.begin():
        task = await s.get(TaskModel, task_id)
        if not task:
            raise RuntimeError(f"任务 {task_id} 不存在")

        task.status = TASK_RUNNING

    return _model_to_dict(task)
```

在 `run_task()`：

```python
task_id, file_dir = await TaskCollection.create_task(...)

await TaskCollection.mark_task_starting(task_id)

handle = TaskHandle(
    serialno=device_id or platform.node(),
    file_dir=file_dir,
    task_id=task_id,
    platform_name=platform.system() if device_type == "pc" else device_type,
    target_pid=pid,
    include_child=include_child,
    device_type=device_type,
    device_id=device_id,
    package_name=package_name,
)

handle.start()

return ok()
```

如果 `handle.start()` 失败：

```python
except Exception as exc:
    if "task_id" in locals():
        try:
            await TaskCollection.fail_task(task_id)
        except Exception:
            pass
    return err(str(exc))
```

这样：

```text
CREATED
  ↓
STARTING/RUNNING
  ↓
不能删除
  ↓
Process start
  ↓
正常结束 -> STOPPED
异常 -> FAILED
```

不需要增加复杂的状态机。

---

## 八、建议补的回归测试

### 1. DataCollect

```python
def test_format_numeric_field_appears_after_none():
    data = [
        {
            "name": "cpu",
            "value": [
                {"time": 1000, "cpu_usage": None},
                {"time": 1001, "cpu_usage": 30},
                {"time": 1002, "cpu_usage": 40},
            ],
        }
    ]

    result = DataCollect._format(data)

    assert result[0]["avg_value"]["cpu_usage"] == 35
    assert result[0]["max_value"]["cpu_usage"] == 40
```

### 2. TaskHandle 异常

模拟采集函数抛异常，验证：

```python
status == TASK_FAILED
```

而不是：

```python
status == TASK_STOPPED
```

### 3. PresentMon

模拟 PresentMon 启动后退出，验证：

```python
WinFps.fps_process is None
```

下一次 FPS 请求可以重新启动。

### 4. 多 GPU

模拟：

```text
GPU0 = 30
GPU1 = 50
```

验证：

```python
gpu == 50
```

### 5. CLI

验证：

```bash
client-perf system-info --device-type ios_simulator
```

可以正常解析。

### 6. Task 生命周期

验证：

```text
create
 ↓
starting
 ↓
delete -> 必须失败
```

以及：

```text
create
 ↓
start
 ↓
stop
 ↓
delete -> 成功
```

以及：

```text
create
 ↓
start failure
 ↓
FAILED
 ↓
delete -> 成功
```

---

## 九、修改范围

```text
client_perf/
├── util.py
│   └── 修复统计字段发现
│
├── task_handle.py
│   └── 正确区分 FAILED / STOPPED
│
├── db.py
│   ├── 增加状态常量
│   ├── 修复 fail_task()
│   └── 增加 STARTING/RUNNING 收敛逻辑
│
├── core/
│   └── pc_tools.py
│       ├── PresentMon 生命周期
│       ├── FPS 启动锁
│       └── 多 GPU 统计
│
├── cli.py
│   └── iOS Simulator system-info
│
└── routers/
    └── tasks.py
        └── Task 启动状态竞态
```

---

## 十、明确不要做

本轮不要：

```text
❌ 去掉 SQLite
❌ 引入 Redis
❌ 引入 Celery
❌ 引入 multiprocessing.Manager
❌ 为 FPS 增加跨进程 IPC
❌ 增加鉴权 / RBAC / Token
❌ 限制 0.0.0.0
❌ 重构整个 Router / Service / Core
❌ 重新抽象所有平台采集器
❌ 为 Singleton 做复杂多进程改造
```

原因：

> 当前设计已经是“一任务一子进程”，Task 之间天然隔离。本轮只修实际影响功能和结果正确性的问题，不改变现有整体架构。

---

## 十一、最终验收标准

### 数据

- [ ] 首个采样值为 `None` 时，后续数值仍能参与统计
- [ ] `avg_value` / `max_value` 不再因为第一行空值而丢失
- [ ] 多 GPU 不再出现后一个 GPU 无条件覆盖前一个 GPU

### FPS

- [ ] PresentMon 正常/异常退出后 `fps_process` 能被清理
- [ ] PresentMon 异常退出后下一次采集可以重新启动
- [ ] 同一个 Task 不会因为启动竞态重复启动多个 PresentMon

### Task

- [ ] 正常结束 → `STOPPED`
- [ ] 采集异常 → `FAILED`
- [ ] Task 尚未真正启动完成时不能被删除
- [ ] Process 启动失败后 DB 不会残留 RUNNING 任务
- [ ] 异常退出任务可以正常删除

### CLI

- [ ] `system-info --device-type ios_simulator` 可以正常解析
- [ ] PC / Android / iOS / Harmony 现有 CLI 行为不受影响

---

## 十二、执行优先级

```text
1. util.py
   ↓
2. task_handle.py + db.py
   ↓
3. pc_tools.py
   ↓
4. cli.py
   ↓
5. routers/tasks.py
   ↓
6. pytest 回归
```

核心目标只有两个：

```text
                    client_perf
                        │
          ┌─────────────┴─────────────┐
          │                           │
       数据可信                    任务可靠
          │                           │
    DataCollect/FPS/GPU          Task 状态生命周期
```

修完以上 6 项后，不建议继续为了“工程完整性”扩大修改范围。
