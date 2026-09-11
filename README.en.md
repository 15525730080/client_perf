# client-perf

`client-perf` is a cross-platform client performance collection and analysis tool for **PC, Android, iOS, HarmonyOS, and iOS Simulator**. It provides device discovery, fixed-process monitoring, task management, time-range labels, multi-task comparisons, screenshots, and Excel report export through a FastAPI service and browser-based UI.

- Chinese documentation: [README.md](README.md)
- Spanish documentation: [README.es-ES.md](README.es-ES.md)

## Features

- Supports Windows, macOS, Linux, Android, iOS, HarmonyOS, and iOS Simulator.
- Uses a fixed main PID as the monitoring target.
- Optionally aggregates the real recursive child-process tree.
- Collects CPU, memory, FPS, GPU, threads, handles, disk I/O, network I/O, and screenshots where supported.
- Never substitutes host-wide network traffic for unavailable process-level network metrics.
- Stores task metadata in SQLite and metric series in per-task CSV files.
- Supports labels, baselines, task comparisons, and Excel exports.
- Provides a Web UI at `http://127.0.0.1:8080` by default.

## Process Scope Semantics

Every collection task is anchored to a **fixed main PID**.

- **Current process only**: metrics are collected only for the selected PID.
- **Include child processes**: metrics are aggregated for the selected PID and all of its recursive operating-system descendants.

Processes with the same application or package name are not automatically included if they are siblings rather than descendants of the selected PID.

For network metrics, `client-perf` only uses a reliable process- or UID-scoped source. If the platform does not expose one, the metric returns `null` instead of falling back to machine-wide traffic.

## Platform Support

| Platform | Discovery | Main collection backend | Screenshot | Notes |
|---|---|---|---|---|
| PC (Windows/macOS/Linux) | Local host | `psutil`, PresentMon, NVML | Pillow | PresentMon FPS collection may require administrator privileges on Windows. |
| Android | ADB | `adbutils`, `/proc`, `dumpsys` | `adb screencap` | Requires a device or emulator visible through `adb devices`. |
| iOS device | go-ios | `py-ios-device`, Instruments, go-ios | go-ios | iOS 17 and later may require a privileged go-ios tunnel. |
| HarmonyOS | HDC | `hdc shell`, `/proc`, system services | `hdc screencap` | Requires HDC in `PATH` or the `HDC_PATH` environment variable. |
| iOS Simulator | `xcrun simctl` | Host process metrics via `psutil` | `simctl io` | Requires macOS and Xcode Command Line Tools. Unsupported process-level metrics return `null`. |

## Metrics

| Metric | Child-process aggregation | Availability notes |
|---|---:|---|
| CPU | Yes | Platform-specific process counters |
| Memory | Yes | Process RSS or platform equivalent |
| Threads | Yes | Aggregated across the target process tree |
| Handles/file descriptors | Yes | Depends on platform support |
| Disk I/O | Yes where supported | Returns `null` when no trustworthy process-level source exists |
| Network I/O | UID/process scoped where supported | Never falls back to host-wide traffic |
| FPS | Platform dependent | PresentMon on Windows; Instruments on supported iOS paths |
| GPU | Platform dependent | NVIDIA NVML on PC and supported mobile instrumentation |
| Screenshot | Device dependent | Captured independently of metric aggregation |

## Requirements

- Python **3.10 or later**
- [uv](https://docs.astral.sh/uv/)
- Platform tools as needed:
  - Android: ADB / Android SDK Platform-Tools
  - HarmonyOS: HDC / DevEco Studio or HarmonyOS SDK
  - iOS device: USB trust relationship; elevated go-ios tunnel for some iOS versions
  - iOS Simulator: macOS with Xcode Command Line Tools

## Clean Installation from Source

The repository uses `uv` as the source of truth for dependency resolution, virtual environments, and builds.

```bash
# Clone and enter the repository
git clone https://github.com/15525730080/client_perf.git
cd client_perf

# Create a fresh .venv, install locked dependencies,
# and install the project in editable mode
uv sync

# Verify the command-line entry point
uv run client-perf --help
```

To include native-build tooling:

```bash
uv sync --extra native
```

To reproduce the exact locked environment, keep `uv.lock` under version control and run `uv sync` without manually installing packages into `.venv`.

## Start the Service

```bash
# Recommended
uv run client-perf

# Custom bind address and port
uv run client-perf --host 127.0.0.1 --port 8080
```

You can also run the module directly inside the uv environment:

```bash
uv run python -m client_perf
```

Open the Web UI:

```text
http://127.0.0.1:8080
```

To store the database, task data, screenshots, and generated reports in a custom location:

```bash
CLIENT_PERF_DATA_DIR=/path/to/client-perf-data uv run client-perf
```

## Platform Setup

### Android

Ensure ADB is available and the target is listed:

```bash
adb devices
```

On macOS, Android Platform-Tools can also be installed with:

```bash
brew install android-platform-tools
```

### HarmonyOS

Install DevEco Studio or the HarmonyOS SDK, then ensure HDC is available:

```bash
hdc list targets
```

If HDC is not in `PATH`, set its absolute path:

```bash
export HDC_PATH=/absolute/path/to/hdc
```

### iOS Devices

Connect the device over USB and trust the computer. For iOS 17 and later, a go-ios tunnel may require administrator or root privileges because it creates a virtual network interface.

### iOS Simulator

Install Xcode Command Line Tools and boot a simulator:

```bash
xcrun simctl list devices available
open -a Simulator
```

The Web UI discovers booted simulators and their running UIKit applications. iOS Simulator CPU, memory, thread, and file-descriptor metrics are collected from the fixed host PID and its optional recursive descendants. Metrics without a reliable process-level source return `null`.

## Usage Workflow

1. Start the service and open the Web UI.
2. Select a PC, connected mobile device, or booted iOS Simulator.
3. Select the target application or process.
4. Choose **Current process only** or **Include child processes**.
5. Start collection and inspect the live metric series.
6. Stop the task when the scenario is complete.
7. Add time-range labels, set a baseline, compare tasks, or export Excel reports.

## API Overview

All endpoints use the response envelope:

```json
{
  "code": 200,
  "msg": "response payload"
}
```

### Devices

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/get_devices/` | List connected and local devices |
| GET | `/platform_capabilities/` | Report platform/tool availability |
| GET | `/system_info/` | Return device system information |
| GET | `/get_pids/` | Return process or application information |
| GET | `/get_device_apps/` | Return applications for a selected device |
| GET | `/pid_img/` | Capture a process window or device screenshot |

### Tasks

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/get_all_task/` | List collection tasks |
| GET | `/run_task/` | Start a task for a fixed PID |
| GET | `/stop_task/` | Stop a task |
| GET | `/task_status/` | Get task status |
| GET | `/result/` | Read collected task data |
| GET | `/delete_task/` | Delete a task |
| GET | `/change_task_name/` | Rename a task |
| GET | `/set_task_version/` | Set task version metadata |
| GET | `/set_task_baseline/` | Mark or unmark a baseline task |

The `/run_task/` endpoint accepts `pid`, `pid_name`, `task_name`, `device_type`, `device_id`, `package_name`, and `include_child`.

### Comparison and Export

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/create_comparison/` | Compare multiple tasks |
| POST | `/export_comparison_excel/` | Export a task comparison |
| POST | `/export_excel/` | Export one task |
| GET | `/get_labels/{task_id}/` | List labels for a task |
| POST | `/create_label_comparison/` | Compare labeled time ranges |
| POST | `/export_label_comparison_excel/` | Export a label comparison |

## Project Layout

```text
client-perf/
├── client_perf/
│   ├── cli.py                    # Command-line entry point
│   ├── api.py                    # FastAPI application
│   ├── routers/                  # API route modules
│   ├── services/                 # Application services
│   ├── db.py                     # Async SQLite persistence
│   ├── task_handle.py            # Collection subprocess management
│   ├── comparison.py             # Comparison and report logic
│   ├── paths.py                  # Runtime path resolution
│   ├── native_build.py           # Optional native build workflow
│   ├── core/
│   │   ├── monitor.py            # Generic collection loop and CSV writer
│   │   ├── device_manager.py     # Unified device discovery and dispatch
│   │   ├── pc_tools.py
│   │   ├── android_tools.py
│   │   ├── ios_tools.py
│   │   ├── ios_simulator_tools.py
│   │   └── harmony_tools.py
│   ├── test_result/index.html    # Web UI
│   └── tool/                     # Bundled platform helper binaries
├── tests/
├── pyproject.toml
├── uv.lock
└── README.md
```

## Development and Validation

```bash
# Install development dependencies from the lock file
uv sync

# Run the test suite
uv run pytest -q tests

# Check the CLI
uv run client-perf --help

# Build wheel and source distribution
uv build
```

Optional native acceleration:

```bash
uv sync --extra native
uv run nativebuild
uv run client-perf native-start
```

## Known Limitations

- iOS Simulator does not currently expose stable process-level FPS, GPU, disk-byte, or network-byte sources through this implementation; those metrics return `null`.
- Android or HarmonyOS process network collection depends on platform-provided UID/process accounting. If it is unavailable or unreadable, network values return `null`.
- Same-package sibling processes are not part of the selected PID's child-process tree.
- NVIDIA GPU collection uses NVML; AMD and Intel GPU utilization is not currently collected through that backend.
- Screenshot support on headless Linux systems may be unavailable.

## License

MIT License

## Author

Fan Bozhou (`fanbozhou`)
15525730080@163.com
