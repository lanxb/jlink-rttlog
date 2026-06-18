# J-Link RTT Logger

[![Build & Release](https://github.com/lanxb/jlink-rttlog/actions/workflows/build.yml/badge.svg)](https://github.com/lanxb/jlink-rttlog/actions/workflows/build.yml)

RTT log capture with auto-reconnect on target power loss. Prints to console and writes to timestamped log files.

## Download

Pre-built binaries for Windows, Linux, and macOS available on the [Releases](https://github.com/lanxb/jlink-rttlog/releases) page.

> Requires Segger J-Link Software Pack installed on the target machine.

## Quick Start

### Windows

```batch
:: setup (first time)
setup.bat

:: run
venv\Scripts\python jlink_rttlog.py

:: or with options
venv\Scripts\python jlink_rttlog.py -s 20630302 -c GD32F303VG
venv\Scripts\python jlink_rttlog.py -i jtag -c STM32F103C8
```

### Linux / macOS

```bash
# setup (first time)
bash setup.sh

# run
venv/bin/python jlink_rttlog.py

# or with options
venv/bin/python jlink_rttlog.py -s 20630302 -c GD32F303VG
venv/bin/python jlink_rttlog.py -i jtag -c STM32F103C8
```

## Usage

```
jlink_rttlog.py [-h] [-s SERIAL] [-i {swd,jtag}] [-c CHIP]
                [--speed SPEED] [--threshold THRESHOLD]
                [--rtt-buffer RTT_BUFFER] [--interval INTERVAL]
```

| Argument | Default | Description |
|---|---|---|
| `-s`, `--serial` | auto | J-Link serial number |
| `-i`, `--interface` | `swd` | Target interface: `swd` or `jtag` |
| `-c`, `--chip` | `GD32F303VG` | Target chip name |
| `--speed` | `4000` | Interface speed (kHz) |
| `--threshold` | auto | Power-loss voltage threshold in mV (default: auto-detect 60% of VTarget) |
| `--rtt-buffer` | `0` | RTT buffer index |
| `--interval` | `0.01` | Poll interval (seconds) |

## Features

### Connection & Recovery

- **Auto-reconnect** — detects target power loss via voltage monitoring, waits for power on, then reconnects automatically
- **App-jump detection** — multi-signal monitoring (VTOR, vector table SP, PC) detects when the MCU jumps to a different application and triggers reconnect
- **RTT CB health check** — periodically validates the RTT control block; lost CB triggers reconnect
- **Multi J-Link** — lists all connected devices, auto-selects available one or specify by serial
- **Serial lock** — auto mode locks to the first selected J-Link for all subsequent reconnects

### Logging

- **Timestamped logs** — new log file per session: `logs/rtt_{chip}_{iface}_{serial}_{timestamp}.log`
- **Log continuation** — on app-jump reconnect, resumes writing to the same log file
- **Console mirror** — RTT data printed to console in real time with ANSI escape codes stripped
- **Path sanitization** — log filenames are sanitized to prevent directory traversal

### Voltage

- **Auto-threshold** — on first connect, reads VTarget and sets power-loss threshold to 60% (minimum 500mV)
- **Manual threshold** — override with `--threshold` for custom power-loss detection

## Build

### Windows

```batch
build.bat
```

### Linux / macOS

```bash
bash build.sh
```

Output: `jlink-rttlog` (or `jlink-rttlog.exe` on Windows)

### CI

Pushing a `v*` tag (e.g. `v0.1.0`) to `master` triggers
automated build + release via GitHub Actions. Binaries are built
on `windows-latest`, `ubuntu-latest`, and `macos-latest` and
attached to the release.

## Development

```bash
# run tests
python -m pytest test_jlink_rttlog.py -v
```

CI runs tests on every push to `master` and every pull request,
across Windows, Linux, and macOS.

## Requirements

- Python 3.10+
- Segger J-Link hardware
- J-Link Software Pack:
  - **Windows**: provides `JLinkARM.dll`
  - **Linux**: provides `libjlinkarm.so`
  - **macOS**: provides `libjlinkarm.dylib`
