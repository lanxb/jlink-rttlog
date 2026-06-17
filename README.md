# J-Link RTT Logger

RTT log capture with auto-reconnect on target power loss. Prints to console and writes to timestamped log files.

## Quick Start

```powershell
# setup (first time)
.\setup.bat

# run
venv\Scripts\python jlink-rttlog.py

# or with options
venv\Scripts\python jlink-rttlog.py -s 20630302 -c GD32F303VG
venv\Scripts\python jlink-rttlog.py -i jtag -c STM32F103C8
```

## Usage

```
jlink-rttlog.py [-h] [-s SERIAL] [-i {swd,jtag}] [-c CHIP]
                [--speed SPEED] [--threshold THRESHOLD]
                [--rtt-buffer RTT_BUFFER] [--interval INTERVAL]
```

| Argument | Default | Description |
|---|---|---|
| `-s`, `--serial` | auto | J-Link serial number |
| `-i`, `--interface` | `swd` | Target interface: `swd` or `jtag` |
| `-c`, `--chip` | `GD32F303VG` | Target chip name |
| `--speed` | `4000` | Interface speed (kHz) |
| `--threshold` | `500` | Power-loss voltage threshold (mV) |
| `--rtt-buffer` | `0` | RTT buffer index |
| `--interval` | `0.01` | Poll interval (seconds) |

## Features

- **Auto-reconnect** — detects target power loss via voltage monitoring and reconnects
- **Multi J-Link** — lists all connected devices, select by serial number
- **Timestamped logs** — new log file on each reconnect: `rtt_{chip}_{iface}_{serial}_{timestamp}.log`
- **Console mirror** — RTT data printed to console in real time while writing to file

## Build EXE

```batch
.\build.bat
```

Output: `jlink-rttlog.exe`

## Requirements

- Python 3.10+
- Segger J-Link hardware
- J-Link Software Pack (provides `JLinkARM.dll`)
