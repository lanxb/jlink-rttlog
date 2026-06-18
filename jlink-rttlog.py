"""
J-Link RTT Logger — capture RTT logs from ARM MCUs via Segger J-Link debug probes.

Features auto-reconnect on target power loss, multi-JLink support,
timestamped log files, and real-time console mirror.
"""

import argparse
import ctypes
try:
    import msvcrt
except ImportError:
    msvcrt = None
import os
import re
import sys
import pylink
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RTT_CB_DETECTION_ATTEMPTS = 60   # 60 × 0.1s = 6s max for RTT control block
RTT_CB_POLL_INTERVAL = 0.1       # seconds between detection attempts
DLL_RELEASE_DELAY = 0.3          # seconds for JLinkARM.dll to release after close
RETRY_DELAY = 2.0                # seconds between reconnect attempts
RTT_STALE_RECHECK = 2.0         # interval for app-jump health probe
RTT_READ_BUFFER_SIZE = 1024      # bytes per RTT read
JLINK_WAIT_POLL_INTERVAL = 2.0   # seconds between J-Link discovery checks

# Windows console constants
CTRL_CLOSE_EVENT = 2
STD_INPUT_HANDLE = -10
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080

# regex for ANSI escape sequences (CSI, OSC, and other control sequences)
_ANSI_ESCAPE_RE = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# interface name -> pylink enum
INTERFACE_MAP = {
    'swd': pylink.enums.JLinkInterfaces.SWD,
    'jtag': pylink.enums.JLinkInterfaces.JTAG,
}

# computed at import time (harmless path calculation)
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_DIR = os.path.join(BASE_DIR, 'logs')


# ---------------------------------------------------------------------------
# Pure utility functions (testable without hardware)
# ---------------------------------------------------------------------------

def _sanitize_path_component(s):
    """Replace path separators and dangerous characters with underscores."""
    return re.sub(r'[/\\:]', '_', str(s))


def _strip_ansi(text):
    """Remove ANSI escape sequences from terminal output."""
    return _ANSI_ESCAPE_RE.sub('', text)


def parse_args():
    """Parse and validate command-line arguments.

    Returns an argparse.Namespace with all options.  Exits with an error
    message when any argument is out of its valid range.
    """
    parser = argparse.ArgumentParser(
        description='J-Link RTT logger with auto-reconnect on power loss',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
examples:
  %(prog)s                                              # auto-select J-Link, default GD32F303VG
  %(prog)s -s 20630302 -c STM32F407VG                   # specify serial and chip
  %(prog)s -s 20630302 -c GD32F303VG --speed 8000       # 8 MHz speed
  %(prog)s -i jtag -c STM32F103C8                       # JTAG interface
  %(prog)s -c GD32F303VG --threshold 1200               # custom power threshold (mV)
        '''
    )
    parser.add_argument('-s', '--serial', type=int, default=None,
                        help='J-Link serial number (default: auto-select first)')
    parser.add_argument('-i', '--interface', default='swd',
                        choices=['swd', 'jtag'],
                        help='target interface swd/jtag (default: swd)')
    parser.add_argument('-c', '--chip', default='GD32F303VG',
                        help='target chip name (default: GD32F303VG)')
    parser.add_argument('--speed', type=int, default=4000,
                        help='interface speed in kHz (default: 4000)')
    parser.add_argument('--threshold', type=int, default=None,
                        help='power-loss voltage threshold in mV'
                             ' (default: auto-detect 60%% of VTarget)')
    parser.add_argument('--rtt-buffer', type=int, default=0,
                        help='RTT buffer index (default: 0)')
    parser.add_argument('--interval', type=float, default=0.01,
                        help='RTT poll interval in seconds (default: 0.01)')
    args = parser.parse_args()

    # validate argument ranges
    if args.speed < 1:
        parser.error('--speed must be >= 1 kHz')
    if args.threshold is not None and args.threshold < 0:
        parser.error('--threshold must be >= 0 mV')
    if args.interval <= 0:
        parser.error('--interval must be > 0 seconds')
    if args.rtt_buffer < 0:
        parser.error('--rtt-buffer must be >= 0')

    return args


def select_jlink(emulators, target_serial, jlink=None):
    """Select a J-Link by serial number, or auto-pick the first available.

    In *specified mode* (target_serial is not None): returns the matching
    serial or raises ValueError so the caller can wait/retry.

    In *auto mode* (target_serial is None): iterates through emulators,
    skipping any that are already opened by another process (detected via
    a quick open/close probe).  Returns the first available serial, or
    raises ValueError if all are occupied.

    Args:
        emulators: list of connected emulator objects from pylink.
        target_serial: int | None — desired serial number.
        jlink: optional pylink.JLink instance for availability probing.
    """
    if target_serial is not None:
        matched = [e for e in emulators if e.SerialNumber == target_serial]
        if matched:
            serial = matched[0].SerialNumber
            print(f"Selected J-Link (serial={serial})\n")
            return serial
        raise ValueError(
            f"J-Link serial={target_serial} not found. "
            f"Available: {[e.SerialNumber for e in emulators]}"
        )

    # Auto mode: try each emulator, skip occupied ones
    for emu in emulators:
        serial = emu.SerialNumber
        if jlink is not None:
            try:
                jlink.open(serial_no=serial)
                jlink.close()
            except Exception as e:
                msg = str(e).lower()
                if 'already open' in msg or 'in use' in msg:
                    print(f"  Skipping occupied J-Link serial={serial}")
                    continue
                raise
        print(f"Auto-selected J-Link (serial={serial})\n")
        return serial

    raise ValueError("All J-Link devices are occupied")


def make_log_filename(chip, interface, serial_number):
    """Generate a sanitised log path: logs/rtt_{chip}_{iface}_{serial}_{ts}.log

    All path components are sanitised to prevent directory traversal.
    Creates the logs/ directory if it does not exist.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    chip = _sanitize_path_component(chip)
    iface = _sanitize_path_component(interface)
    serno = _sanitize_path_component(serial_number)
    return os.path.join(LOG_DIR, f'rtt_{chip}_{iface}_{serno}_{ts}.log')


# ---------------------------------------------------------------------------
# RttLogger — encapsulates all J-Link state and operations
# ---------------------------------------------------------------------------

class RttLogger:
    """Manages a J-Link connection and RTT logging session.

    Handles connection lifecycle, auto-reconnect on power loss,
    voltage monitoring, and graceful shutdown.  All state (J-Link
    handle, shutdown flag) is scoped to the instance so the module
    can be imported without side effects.
    """

    def __init__(self, args):
        self.args = args
        self.jlink = pylink.JLink()
        self._shutdown = False
        self._cleaned_up = False
        self._log_filename = None
        self._keep_log = False   # True → reuse log file on next reconnect
        # App-jump detection baselines (set after connect)
        self._expected_vtor = None     # VTOR register value
        self._expected_vt_sp = None    # vector table initial SP
        self._reset_handler = None     # firmware reset handler (vector[1])
        self._setup_console()

    # -- console helpers ---------------------------------------------------

    def _setup_console(self):
        """Register Windows console control handler and disable Quick Edit.

        Only active on win32.  The ctypes callback is stored on *self* so
        it cannot be garbage-collected while the handler is registered.
        """
        if sys.platform != 'win32':
            return

        _HR = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong)

        @_HR
        def _on_close(ctrl_type):
            self._shutdown = True
            return 1 if ctrl_type == CTRL_CLOSE_EVENT else 0

        ctypes.windll.kernel32.SetConsoleCtrlHandler(_on_close, True)
        self._on_close = _on_close  # prevent GC of the ctypes callback

        # disable Quick Edit mode to prevent accidental click-to-pause
        h_in = ctypes.windll.kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_ulong()
        ctypes.windll.kernel32.GetConsoleMode(h_in, ctypes.byref(mode))
        ctypes.windll.kernel32.SetConsoleMode(
            h_in, mode.value & ~ENABLE_QUICK_EDIT_MODE)

    def _should_exit(self):
        """Return True when the user has requested shutdown.

        Checks the shutdown flag (set by the console close handler on
        Windows) and the console input buffer for Ctrl+C / Ctrl+Z on
        Windows, or any keypress on POSIX.
        """
        if self._shutdown:
            return True
        if msvcrt:
            # Windows: check for Ctrl+C / Ctrl+Z via msvcrt kbhit
            if msvcrt.kbhit():
                return msvcrt.getch() in (b'\x03', b'\x1a')
            return False
        # POSIX: non-blocking check for any stdin input (any key = exit)
        import select
        if select.select([sys.stdin], [], [], 0)[0]:
            try:
                sys.stdin.read(1)
            except Exception:
                pass
            return True
        return False

    # -- J-Link lifecycle --------------------------------------------------

    def _cleanup(self):
        """Stop RTT and close the J-Link connection.  Idempotent — safe to
        call multiple times."""
        if self._cleaned_up:
            return
        self._cleaned_up = True
        print("\nShutting down...")
        self._disconnect()

    def _disconnect(self):
        """Disconnect from the target: stop RTT then close the J-Link.

        Failures are logged to stderr but never raised — this is a
        best-effort teardown so that one failing step does not prevent
        the next.
        """
        try:
            self.jlink.rtt_stop()
        except Exception as e:
            print(f"[!] rtt_stop failed: {e}", file=sys.stderr)

        try:
            if self.jlink.opened():
                self.jlink.close()
        except Exception as e:
            print(f"[!] close failed: {e}", file=sys.stderr)

    def list_emulators(self):
        """List all connected J-Link devices (prints to console).

        Returns:
            list — connected emulator objects, or empty list if none found.
        """
        emulators = self.jlink.connected_emulators()
        if not emulators:
            return []
        print(f"\nFound {len(emulators)} J-Link device(s):")
        for i, emu in enumerate(emulators):
            product = emu.acProduct.decode().strip('\x00')
            print(f"  [{i}] {product}  serial={emu.SerialNumber}"
                  f"  conn={emu.Connection}")
        return emulators

    def _connect_jlink(self, serial):
        """Open the J-Link, configure the interface, and connect to the target.

        Ensures a clean state by disconnecting any previous session first.
        Disables driver dialog boxes so the CLI is never blocked by a popup.
        """
        if self.jlink.opened():
            self._disconnect()
            time.sleep(DLL_RELEASE_DELAY)

        self.jlink.open(serial_no=serial)
        self.jlink.disable_dialog_boxes()
        self.jlink.set_tif(INTERFACE_MAP[self.args.interface])
        self.jlink.connect(self.args.chip, speed=self.args.speed)

    def _wait_for_rtt_cb(self):
        """Start RTT and poll for the control block via ``rtt_get_buf_descriptor``.

        Returns:
            bool — True if the control block was found (or we proceed anyway),
                   False if the user requested shutdown during the wait.
        """
        self.jlink.rtt_start()
        for _ in range(RTT_CB_DETECTION_ATTEMPTS):
            if self._should_exit():
                return False
            try:
                self.jlink.rtt_get_buf_descriptor(0, True)
                print("RTT control block found, ready.")
                return True
            except Exception:
                pass  # not found yet — keep polling
            time.sleep(RTT_CB_POLL_INTERVAL)

        print("RTT control block not found, will keep trying...")
        return True  # proceed — rtt_read will retry

    def _check_rtt_cb_alive(self):
        """Quick check: is the RTT control block still accessible?

        Uses ``rtt_get_buf_descriptor`` which raises if the control block
        has moved or become invalid (e.g. after an app jump).
        """
        try:
            self.jlink.rtt_get_buf_descriptor(0, True)
            return True
        except Exception:
            return False

    def _read_vector_word(self, vtor, offset):
        """Read a 4-byte word from the vector table at ``vtor + offset``.

        Returns the word as an int, or None on failure.
        """
        try:
            data = self.jlink.memory_read(vtor + offset, 4)
            return int.from_bytes(bytes(data), byteorder='little')
        except Exception:
            return None

    def _read_vtor(self):
        """Read the VTOR (Vector Table Offset Register) at 0xE000ED08.

        VTOR tells us where the current vector table is in memory.
        Available on Cortex-M3/M4/M7/M33.  Returns None on Cortex-M0
        or if the read fails.
        """
        try:
            data = self.jlink.memory_read(0xE000ED08, 4)
            return int.from_bytes(bytes(data), byteorder='little')
        except Exception:
            return None

    def _read_vector_sp(self, vtor):
        """Read the initial SP from the vector table at ``vtor + 0x00``."""
        return self._read_vector_word(vtor, 0x00)

    def _read_reset_handler(self, vtor):
        """Read the reset handler from the vector table at ``vtor + 0x04``."""
        addr = self._read_vector_word(vtor, 0x04)
        return addr if addr and addr != 0 else None

    def _detect_app_jump(self):
        """Check multiple signals to detect if the MCU has jumped to a
        different application.

        Signal 1 — VTOR change (most reliable on Cortex-M3/M4/M7):
          If the vector table offset register has changed, a different
          app has taken over.  Exact comparison, no threshold needed.

        Signal 2 — Vector table SP change:
          If the SP value at address 0x00 has changed, the vector table
          has been overwritten by a different app.  Exact comparison.

        Signal 3 — PC far from reset handler (fallback):
          If the PC has moved very far from the firmware's entry point,
          the MCU is executing code in a different region.  Uses a
          generous threshold (2 MB) to avoid false positives on chips
          with large flash.

        Returns:
            str — reason string if app jump detected, None otherwise.
        """
        vtor = self._read_vtor()  # read once, used by signals 1 & 2

        # Signal 1: VTOR changed
        if self._expected_vtor is not None:
            if vtor is not None and vtor != self._expected_vtor:
                return (f"VTOR changed"
                        f" (0x{self._expected_vtor:08X} → 0x{vtor:08X})")

        # Signal 2: Vector table SP changed
        if vtor is not None and self._expected_vt_sp is not None:
            vt_sp = self._read_vector_sp(vtor)
            if vt_sp is not None and vt_sp != self._expected_vt_sp:
                return (f"vector table changed"
                        f" (SP 0x{self._expected_vt_sp:08X}"
                        f" → 0x{vt_sp:08X})")

        # Signal 3: PC far from firmware (fallback)
        if self._reset_handler is not None:
            try:
                pc = self.jlink.register_read("PC")
                if pc is not None:
                    delta = abs(pc - self._reset_handler)
                    if delta > 0x200000:  # > 2 MB from firmware entry
                        return (f"PC=0x{pc:08X} far from firmware"
                                f" (reset=0x{self._reset_handler:08X},"
                                f" delta=0x{delta:X})")
            except Exception:
                pass  # register read can fail transiently

        return None

    # -- main read loop ----------------------------------------------------

    def _read_loop(self):
        """Inner RTT read loop.  Monitors voltage, reads RTT data, and writes
        to the log file.

        Periodically checks for app jumps via VTOR, vector table SP, and PC
        validation.  Also monitors the RTT control block health.

        Returns:
            bool — True if the caller should reconnect (power loss / error),
                   False if the caller should shut down.
        """
        recheck_interval = max(1, int(RTT_STALE_RECHECK / self.args.interval))
        cb_was_alive = False
        ticks_since_recheck = 0
        rtt_buf = self.args.rtt_buffer

        while True:
            if self._should_exit():
                return False  # user requested shutdown

            # --- voltage check ---
            try:
                v = self.jlink.hardware_status.voltage
            except Exception as e:
                print(f"[!] Voltage read error: {e}", file=sys.stderr)
                v = 0

            if v < self.args.threshold:
                print(f"Power lost! (VTarget={v}mV) Waiting for power on...")
                self._disconnect()
                time.sleep(DLL_RELEASE_DELAY)
                return True  # reconnect

            # --- RTT control block health check (every recheck_interval) ---
            if ticks_since_recheck >= recheck_interval:
                ticks_since_recheck = 0
                alive = self._check_rtt_cb_alive()

                if cb_was_alive and not alive:
                    # CB was reachable but now it's gone —
                    # almost certainly an app jump or target reset
                    print("RTT control block lost (app jump?), reconnecting...")
                    self._disconnect()
                    time.sleep(DLL_RELEASE_DELAY)
                    return True  # full reconnect

                if alive:
                    if not cb_was_alive:
                        print("RTT control block detected, reading...")
                    cb_was_alive = True
                    # update bounds check when we know buffers exist
                    try:
                        num_bufs = self.jlink.rtt_get_num_up_buffers()
                    except Exception:
                        num_bufs = 0
                    if self.args.rtt_buffer >= num_bufs and num_bufs > 0:
                        print(f"[!] RTT buffer {self.args.rtt_buffer} out of"
                              f" range (0–{num_bufs - 1}), falling back to 0")
                        rtt_buf = 0
                    else:
                        rtt_buf = self.args.rtt_buffer

                # --- App jump detection ---
                # Multi-signal check: VTOR change, vector table change,
                # or PC far from firmware region.
                reason = self._detect_app_jump()
                if reason:
                    print(f"App jump detected ({reason}), reconnecting...")
                    self._keep_log = True
                    self._disconnect()
                    time.sleep(DLL_RELEASE_DELAY)
                    return True  # full reconnect
            ticks_since_recheck += 1

            # --- RTT read ---
            try:
                data = self.jlink.rtt_read(rtt_buf, RTT_READ_BUFFER_SIZE)
            except Exception as e:
                print(f"Connection lost: {e}, reconnecting...")
                self._disconnect()
                time.sleep(DLL_RELEASE_DELAY)
                return True  # reconnect

            if data:
                # decode for console (strip ANSI), write raw bytes to file
                text = bytes(data).decode('utf-8', errors='replace')
                print(_strip_ansi(text), end='', flush=True)
                with open(self._log_filename, 'ab') as f:
                    f.write(bytes(data))

            time.sleep(self.args.interval)

    # -- public entry point ------------------------------------------------

    def run(self, serial):
        """Main reconnect loop.

        Connects to the J-Link, initialises RTT, and enters the read loop.
        Automatically reconnects after power loss or connection errors.
        Guarantees cleanup on every exit path (shutdown, Ctrl+C, exception).
        """
        try:
            while True:
                if self._should_exit():
                    return

                try:
                    self._connect_jlink(serial)
                except Exception as e:
                    print(f"Error: {e}, retrying in {RETRY_DELAY:.0f}s...")
                    self._disconnect()
                    time.sleep(RETRY_DELAY)
                    continue

                print(f"Connected ({self.args.chip},"
                      f" {self.args.interface.upper()},"
                      f" {self.args.speed}kHz). Starting RTT...")

                # Auto-detect voltage threshold if user didn't specify one
                if self.args.threshold is None:
                    try:
                        v_target = self.jlink.hardware_status.voltage
                    except Exception:
                        v_target = 3300  # sensible default if read fails
                    self.args.threshold = max(500, int(v_target * 0.6))
                    print(f"VTarget={v_target}mV,"
                          f" auto-threshold={self.args.threshold}mV")

                if not self._wait_for_rtt_cb():
                    return  # shutdown requested

                # Read baselines for app-jump detection.
                self._expected_vtor = self._read_vtor()
                self._expected_vt_sp = None
                self._reset_handler = None
                if self._expected_vtor is not None:
                    self._expected_vt_sp = self._read_vector_sp(
                        self._expected_vtor)
                    self._reset_handler = self._read_reset_handler(
                        self._expected_vtor)

                signals = []
                if self._expected_vtor is not None:
                    signals.append(f"VTOR=0x{self._expected_vtor:08X}")
                if self._expected_vt_sp is not None:
                    signals.append(f"VT.SP=0x{self._expected_vt_sp:08X}")
                if self._reset_handler is not None:
                    signals.append(f"Reset=0x{self._reset_handler:08X}")
                if signals:
                    print(f"App-jump baseline: {', '.join(signals)}")
                else:
                    print("[!] Could not read vector table,",
                          " app-jump detection limited")

                if not self._keep_log:
                    self._log_filename = make_log_filename(
                        self.args.chip, self.args.interface, serial)
                    print(f"Writing RTT log to: {self._log_filename}")
                else:
                    print(f"Reconnected, continuing: {self._log_filename}")
                self._keep_log = False

                should_reconnect = self._read_loop()
                if not should_reconnect:
                    return
                # loop back to _connect_jlink (reconnect)

        except KeyboardInterrupt:
            print()
        finally:
            self._cleanup()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = parse_args()
    logger = RttLogger(args)

    try:
        while True:
            emulators = logger.list_emulators()
            while not emulators:
                if logger._should_exit():
                    print("\nCancelled.")
                    logger._cleanup()
                    sys.exit(0)
                print("Waiting for J-Link... (Ctrl+C to exit)")
                time.sleep(JLINK_WAIT_POLL_INTERVAL)
                emulators = logger.list_emulators()

            try:
                serial = select_jlink(emulators, args.serial,
                                     jlink=logger.jlink)
                # Auto mode: lock to the selected serial for all reconnects
                if args.serial is None:
                    args.serial = serial
                break  # serial found, proceed
            except ValueError as e:
                print(f"[!] {e}")
                if args.serial is not None:
                    # Specified mode: keep waiting for the target serial
                    print(f"Retrying for serial={args.serial}...\n")
                    time.sleep(RETRY_DELAY)
                    continue
                # Auto mode shouldn't reach here, but safety fallback
                print("No emulators available, retrying...\n")
                time.sleep(RETRY_DELAY)

        logger.run(serial)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        logger._cleanup()
