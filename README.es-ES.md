# client-perf

Herramienta de recolección y análisis de rendimiento del cliente, compatible con las cuatro grandes plataformas: **PC / Android / iOS / HarmonyOS**. Ofrece capacidades de recolección de métricas de rendimiento multidimensionales, gestión de tareas, comparación de datos y análisis visual.

- Documentación china: [README.md](README.md)
- English documentation: [README.en.md](README.en.md)

---

## Capturas de Pantalla / Screenshots

<details>
<summary><b>Haga clic para expandir todas las capturas / Click to expand all screenshots</b></summary>

**Lista de Tareas / Task List**
<img width="2558" height="780" alt="image" src="https://github.com/user-attachments/assets/4df2b086-15ec-4610-9f87-025c39b2eee0" />

**Crear Tarea / Create Task**
<img width="2552" height="1184" alt="image" src="https://github.com/user-attachments/assets/7e482cb0-cfe9-416e-a147-016ccd80c9af" />

**Informe de Rendimiento de Aplicación PC / PC App Performance Report**
<img width="2533" height="1256" alt="image" src="https://github.com/user-attachments/assets/d74252df-4dec-4971-b905-52933db65e23" />

**Informe de Rendimiento de Aplicación iOS / iOS App Performance Report**
<img width="2548" height="1284" alt="image" src="https://github.com/user-attachments/assets/ab95909d-f954-4715-8072-8c96bee064bb" />

**Agregar Etiquetas y Comparar Contenido Seleccionado / Add Labels & Compare Selected Content**
<img width="2544" height="1245" alt="image" src="https://github.com/user-attachments/assets/811ccde7-2d79-47d3-ab2b-9424dbf50337" />
<img width="2557" height="743" alt="image" src="https://github.com/user-attachments/assets/727738fb-c726-492e-b23d-d8f1b9356fa0" />
<img width="2556" height="1269" alt="image" src="https://github.com/user-attachments/assets/b2d991e7-299d-4265-acfc-d948094d4976" />

**Lista de Resultados de Comparación / Comparison Results List**
<img width="2560" height="608" alt="image" src="https://github.com/user-attachments/assets/94c63fa2-ea0a-4a9b-982c-05530ef8d7a3" />

</details>

---

## Arquitectura del Sistema / System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Web UI (Navegador / Browser)                    │
│                        http://localhost:8080                                 │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │ HTTP / REST API
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Capa de Servicio FastAPI (api.py)                  │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │
│  │ Gestión  │ │ Gestión  │ │ Análisis  │ │ Gestión  │ │ Exportar Excel   │  │
│  │ Disposit │ │ Tareas   │ │ Comparación│ │ Etiquetas │ │                 │  │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────────┬─────────┘  │
└───────┼────────────┼────────────┼────────────┼────────────────┼─────────────┘
        │            │            │            │                │
        ▼            ▼            ▼            ▼                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Capa de Recolección Core (core/)                    │
│                                                                             │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐  ┌──────────────────┐   │
│  │  pc_tools   │  │android_tools │  │ ios_tools  │  │ harmony_tools    │   │
│  │             │  │              │  │            │  │                  │   │
│  │ psutil      │  │ adbutils     │  │ go-ios     │  │ hdc              │   │
│  │ pynvml      │  │              │  │ py-ios-dev │  │                  │   │
│  │ PresentMon  │  │              │  │            │  │                  │   │
│  └──────┬──────┘  └──────┬───────┘  └─────┬──────┘  └────────┬─────────┘   │
│         │                │                │                   │             │
│         ▼                ▼                ▼                   ▼             │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                      Bucle de Recolección Monitor (monitor.py)        │   │
│   │ CPU → Memoria → FPS → GPU → Hilos → Handles → IO Disco → IO Red → Captura │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Capa de Almacenamiento (db.py)                     │
│                                                                             │
│   SQLite (aiosqlite)                                                        │
│   ├── tasks          Tabla de tareas                                         │
│   ├── comparisons    Tabla de reportes de comparación                        │
│   └── labels         Tabla de etiquetas                                      │
│                                                                             │
│   Archivos CSV (almacenados por directorio de tarea)                        │
│   ├── cpu.csv / memory.csv / fps.csv / gpu.csv / ...                        │
│   └── screenshot/  Directorio de capturas de pantalla                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Características / Features

### Soporte Multiplataforma / Multi-Platform Support

| Plataforma | Descubrimiento de Dispositivos | Lista de Procesos | Recolección de Rendimiento | Captura de Pantalla | Notas |
|------|----------|----------|----------|------|------|
| **PC** (Windows/macOS/Linux) | Local | psutil | psutil + PresentMon | PIL ImageGrab | Requiere permisos de administrador para FPS |
| **Android** | adb | adb shell | adb shell dumpsys | adb screencap | Requiere conexión adb |
| **iOS** | go-ios | go-ios apps | py-ios-device + go-ios tunnel | go-ios screenshot | iOS 17+ requiere go-ios tunnel |
| **HarmonyOS** | hdc | hdc shell | hdc shell | hdc screencap | Requiere herramienta hdc |

### Métricas de Rendimiento / Performance Metrics

| Métrica | Descripción | Método de Recolección | Requisito de Permiso |
|------|------|----------|----------|
| **Uso de CPU** | Uso de CPU del proceso (admite agregación de procesos hijos) | psutil / adb / Instruments | Ninguno |
| **Uso de Memoria** | Memoria RSS del proceso (admite agregación de procesos hijos) | psutil / adb / Instruments | Ninguno |
| **FPS** | Tasa de fotogramas de la aplicación | PresentMon (Windows) / Instruments (iOS) | **Administrador requerido en Windows** |
| **Uso de GPU** | Utilización de GPU NVIDIA | pynvml (NVML API) | Ninguno |
| **Número de Hilos** | Hilos del proceso (admite agregación de procesos hijos) | psutil / adb / Instruments | Ninguno |
| **Número de Handles** | Handles del proceso (Windows) | psutil | Ninguno |
| **IO de Disco** | Tasa de lectura/escritura (MB/s) | psutil io_counters | Ninguno |
| **IO de Red** | Tasa de envío/recepción (MB/s) | psutil net_io_counters | Ninguno |
| **Captura de Pantalla** | Captura de la ventana de la aplicación | PIL / adb / go-ios / hdc | Ninguno |

---

## Instalación / Installation

### Instalación Básica / Basic Installation

El proyecto usa [uv](https://docs.astral.sh/uv/) para gestionar el entorno, las dependencias y la construcción:

```bash
# Crear .venv, instalar dependencias e instalar el proyecto en modo editable
uv sync

# Ejecutar comandos del proyecto
uv run client-perf --help
```

Para instalar una versión publicada desde el índice de paquetes:

```bash
uv tool install client-perf
```

### Dependencias de Plataforma / Platform Dependencies

Dependiendo de la plataforma de prueba, es posible que deban instalarse herramientas adicionales:

#### Plataforma Android / Android Platform

```bash
# Asegúrese de que adb esté disponible
# Método 1: Instalar vía Android SDK
# Método 2: Gestor de paquetes del sistema
#   macOS: brew install android-platform-tools
#   Ubuntu: sudo apt install adb
#   Windows: Descargar Android SDK Platform-Tools
```

#### Plataforma iOS / iOS Platform

```bash
# Las dependencias relacionadas con iOS ya están integradas en el proyecto, no es necesaria configuración adicional
```

#### Plataforma HarmonyOS / HarmonyOS Platform

```bash
# Instale DevEco Studio o HarmonyOS SDK
# Asegúrese de que la herramienta hdc esté en el PATH, o configure la variable de entorno HDC_PATH
```

---

## Inicio Rápido / Quick Start

### Iniciar Servicio / Start Service

```bash
# Escucha por defecto en 0.0.0.0:8080
python -m client_perf

# Especificar dirección de escucha
python -m client_perf --host 127.0.0.1 --port 8080
```

### Permisos de Administrador / Administrator Permission

La recolección de algunas métricas de rendimiento requiere **privilegios de administrador/root**:

| Escenario | Razón | Solución |
|------|------|----------|
| **Recolección de FPS en Windows** | PresentMon necesita acceso al controlador de GPU y a la interfaz DirectX | Ejecutar como administrador |
| **tunnel iOS 17+** | go-ios tunnel necesita crear una interfaz de red virtual y red de espacio de usuario | Ejecutar como administrador/root |

### Acceder a la Interfaz / Access Interface

Después de iniciar el servicio, acceda en el navegador:
```
http://localhost:8080
```

---

## Flujo de Uso / Usage Workflow

### 1. Descubrir Dispositivos / Discover Devices

Después de iniciar el servicio, visite `http://localhost:8080` para ver la lista de dispositivos conectados.

- **PC**: Se muestra automáticamente como máquina local.
- **Android**: Requiere que sea visible mediante `adb devices`.
- **iOS**: Requiere conexión USB y que el dispositivo confíe en la computadora.
- **HarmonyOS**: Requiere que sea visible mediante `hdc list targets`.

### 2. Crear Tarea de Recolección / Create Collection Task

1. Seleccione el dispositivo y la aplicación/proceso objetivo.
2. Ingrese el nombre de la tarea.
3. Elija si desea incluir procesos hijos (agregar métricas de CPU/memoria, etc., de los procesos hijos).
4. Haga clic en iniciar recolección.

### 3. Ver Datos / View Data

Durante la ejecución de la tarea, puede ver las curvas de datos de varias métricas de rendimiento en tiempo real.

### 4. Etiquetas y Comparación / Labels & Comparison

1. En la página de datos de la tarea, puede agregar etiquetas para períodos de tiempo específicos.
2. Seleccione múltiples tareas o etiquetas para comparar.
3. El sistema generará informes de comparación y archivos Excel.

### 5. Exportar Informes / Export Reports

- **Informe de tarea individual**: Exportar como Excel, una hoja de cálculo por métrica.
- **Comparación de múltiples tareas**: Exportar resultados de comparación, incluyendo el resumen y los datos originales de cada tarea.
- **Comparación de etiquetas**: Exportar resultados de comparación dentro de los períodos de tiempo etiquetados.

---

## Estructura del Proyecto / Project Structure

```
client-perf/
├── client_perf/                # Código de la aplicación core
│   ├── __main__.py             # Punto de entrada de inicio
│   ├── api.py                  # Rutas de FastAPI y definiciones de API
│   ├── db.py                   # Operaciones de base de datos SQLite
│   ├── comparison.py           # Lógica de análisis de comparación
│   ├── task_handle.py          # Gestión del proceso de recolección de tareas
│   ├── util.py                 # Utilidades de recolección de datos
│   ├── log.py                  # Configuración de registro (logging)
│   ├── core/                   # Implementaciones de recolección por plataforma
│   │   ├── monitor.py          # Bucle de recolección genérico (escribe en CSV)
│   │   ├── device_manager.py   # Gestión unificada de dispositivos
│   │   ├── pc_tools.py         # Plataforma PC (psutil + PresentMon + pynvml)
│   │   ├── android_tools.py    # Plataforma Android (adb)
│   │   ├── ios_tools.py        # Plataforma iOS (go-ios + py-ios-device)
│   │   └── harmony_tools.py    # Plataforma HarmonyOS (hdc)
│   ├── test_result/            # Interfaz frontend
│   │   └── index.html
│   └── tool/                   # Herramientas integradas
│       ├── PresentMon-1.8.0-*.exe  # Recolección de FPS en Windows
│       └── go-ios-bin/             # Binarios multiplataforma de go-ios
├── setup.py                    # Configuración de empaquetado
├── requirements.txt            # Lista de dependencias
└── README.md                   # Documentación del proyecto
```

---

## Endpoints de la API / API Endpoints

### Gestión de Dispositivos / Device Management

| Método | Ruta | Descripción | Parámetros |
|------|------|------|------|
| GET | `/get_devices/` | Obtener todos los dispositivos conectados | Ninguno |
| GET | `/platform_capabilities/` | Obtener capacidades de plataforma compatibles en el entorno actual | Ninguno |
| GET | `/system_info/` | Obtener información del sistema del dispositivo | `device_type`, `device_id` |
| GET | `/get_pids/` | Obtener lista de procesos/aplicaciones | `device_type`, `device_id`, `is_print_tree` |
| GET | `/get_device_apps/` | Obtener lista de aplicaciones del dispositivo | `device_type`, `device_id` |
| GET | `/pid_img/` | Obtener captura de pantalla | `device_type`, `device_id`, `pid` |

### Gestión de Tareas / Task Management

| Método | Ruta | Descripción | Parámetros |
|------|------|------|------|
| GET | `/get_all_task/` | Obtener todas las tareas | Ninguno |
| GET | `/run_task/` | Iniciar tarea de recolección | `pid`, `pid_name`, `task_name`, `device_type`, `device_id`, `package_name`, `include_child` |
| GET | `/stop_task/` | Detener tarea de recolección | `task_id` |
| GET | `/task_status/` | Obtener estado de la tarea | `task_id` |
| GET | `/result/` | Obtener datos de la tarea | `task_id` |
| GET | `/delete_task/` | Eliminar tarea | `task_id` |
| GET | `/change_task_name/` | Renombrar tarea | `task_id`, `new_name` |
| GET | `/set_task_version/` | Establecer versión de la tarea | `task_id`, `version` |
| GET | `/set_task_baseline/` | Establecer tarea como línea base | `task_id`, `is_baseline` |

### Análisis de Comparación / Comparison Analysis

| Método | Ruta | Descripción | Parámetros |
|------|------|------|------|
| POST | `/create_comparison/` | Crear comparación de múltiples tareas | Cuerpo JSON |
| POST | `/export_comparison_excel/` | Exportar reporte de comparación | Cuerpo JSON |
| POST | `/export_excel/` | Exportar reporte de tarea individual | Cuerpo JSON |

### Gestión de Etiquetas / Label Management

| Método | Ruta | Descripción | Parámetros |
|------|------|------|------|
| GET | `/get_labels/{task_id}/` | Obtener etiquetas de la tarea | `task_id` |
| POST | `/create_label_comparison/` | Crear comparación de etiquetas | Cuerpo JSON |
| POST | `/export_label_comparison_excel/` | Exportar reporte de comparación de etiquetas | Cuerpo JSON |

### Formato de Respuesta Unificado / Unified Response Format

Todos los endpoints devuelven un formato unificado:
```json
{
  "code": 200,
  "msg": <data>
}
```

---

## Dependencias / Dependencies

### Dependencias de Python / Python Dependencies

```
fastapi>=0.111.0          # Framework Web
uvicorn[standard]>=0.29.0 # Servidor ASGI
sqlalchemy[asyncio]>=2.0.0 # ORM + motor async
aiosqlite>=0.20.0         # Controlador async de SQLite
py-ios-device>=2.0.0      # Protocolo iOS Instruments DTX
adbutils>=2.8.0           # Herramienta Android ADB
psutil>=5.9.0             # Recolección de info del sistema PC
pynvml>=11.5.0            # GPU NVIDIA (opcional)
openpyxl>=3.1.0           # Exportación de Excel
Pillow>=10.0.0            # Captura de pantalla PC (opcional)
mypy>=1.14                # Compilación nativa mypyc (incluida en mypy)
```

### Herramientas Externas / External Tools

| Herramienta | Propósito | Plataforma |
|------|------|------|
| **PresentMon** | Recolección de FPS en Windows (integrado) | Windows |
| **go-ios** | Comunicación con dispositivo iOS (integrado + requiere instalación externa) | macOS/Linux/Windows |
| **adb** | Comunicación con dispositivo Android | Todas las plataformas |
| **hdc** | Comunicación con dispositivo HarmonyOS | Todas las plataformas |

---

## Preguntas Frecuentes / FAQ

### P: ¿La recolección de FPS falla en Windows?

R: PresentMon requiere privilegios de administrador. Ejecute `client-perf` como administrador, o no utilice el parámetro `--no-elevate` al iniciar.

### P: ¿La recolección de dispositivos iOS 17+ falla?

R: iOS 17+ requiere conexión a través de go-ios tunnel. Asegúrese de:
1. Ejecutar como administrador/root.
2. Que el dispositivo confíe en la computadora.

### P: ¿No se puede obtener el uso de la GPU?

R: Actualmente solo se admite GPU NVIDIA (a través de pynvml). Las GPU AMD/Intel aún no son compatibles.

### P: ¿La captura de pantalla no está disponible en Linux?

R: Cuando Linux no tiene un entorno de escritorio, PIL ImageGrab no está disponible y la función de captura de pantalla se desactivará automáticamente.

---

## Licencia / License

MIT License

---

## Autor / Author

范博洲(fanbozhou)、15525730080@163.com
