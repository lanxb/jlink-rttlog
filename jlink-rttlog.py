"""
J-Link RTT Logger — capture RTT logs from ARM MCUs via Segger J-Link debug probes.

Features auto-reconnect on target power loss, multi-JLink support,
timestamped log files, and real-time console mirror.
"""

import argparse
import ctypes
import msvcrt
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
    parser.add_argument('--threshold', type=int, default=500,
                        help='power-loss voltage threshold in mV (default: 500)')
    parser.add_argument('--rtt-buffer', type=int, default=0,
                        help='RTT buffer index (default: 0)')
    parser.add_argument('--interval', type=float, default=0.01,
                        help='RTT poll interval in seconds (default: 0.01)')
    args = parser.parse_args()

    # validate argument ranges
    if args.speed < 1:
        parser.error('--speed must be >= 1 kHz')
    if args.threshold < 0:
        parser.error('--threshold must be >= 0 mV')
    if args.interval <= 0:
        parser.error('--interval must be > 0 seconds')
    if args.rtt_buffer < 0:
        parser.error('--rtt-buffer must be >= 0')

    return args


def select_jlink(emulators, target_serial):
    """Select a J-Link by serial number, or auto-pick the first available.

    Args:
        emulators: list of connected emulator objects from pylink.
        target_serial: int | None — desired serial number.

    Returns:
        int — the serial number of the selected emulator.
    """
    if target_serial is not None:
        matched = [e for e in emulators if e.SerialNumber == target_serial]
        if matched:
            serial = matched[0].SerialNumber
            print(f"Selected J-Link (serial={serial})\n")
            return serial
        print(f"[!] J-Link serial={target_serial} not found, using first available")

    serial = emulators[0].SerialNumber
    print(f"Auto-selected J-Link (serial={serial})\n")
    return serial


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

        Checks the shutdown flag (set by the console close handler)
        and the console input buffer for Ctrl+C / Ctrl+Z.
        """
        if self._shutdown:
            return True
        if not msvcrt.kbhit():
            return False
        return msvcrt.getch() in (b'\x03', b'\x1a')

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
        """Poll for the RTT control block on the target.

        Returns:
            bool — True if the RTT subsystem is ready (or we proceed anyway),
                   False if the user requested shutdown during the wait.
        """
        self.jlink.rtt_start()
        for _ in range(RTT_CB_DETECTION_ATTEMPTS):
            if self._should_exit():
                return False
            try:
                if self.jlink.rtt_get_num_up_buffers() > 0:
                    print("RTT control block found, ready.")
                    return True
            except Exception as e:
                print(f"[!] RTT probe error: {e}", file=sys.stderr)
            time.sleep(RTT_CB_POLL_INTERVAL)

        print("RTT control block not found, will keep trying...")
        return True  # proceed — rtt_read will retry

    # -- main read loop ----------------------------------------------------

    def _read_loop(self):
        """Inner RTT read loop.  Monitors voltage, reads RTT data, and writes
        to the log file.

        Returns:
            bool — True if the caller should reconnect (power loss / error),
                   False if the caller should shut down.
        """
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

            # --- RTT buffer bounds check ---
            try:
                num_bufs = self.jlink.rtt_get_num_up_buffers()
            except Exception:
                num_bufs = 0

            if self.args.rtt_buffer >= num_bufs and num_bufs > 0:
                print(f"[!] RTT buffer {self.args.rtt_buffer} out of range"
                      f" (0–{num_bufs - 1}), falling back to buffer 0")
                rtt_buf = 0
            else:
                rtt_buf = self.args.rtt_buffer

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

                if not self._wait_for_rtt_cb():
                    return  # shutdown requested

                self._log_filename = make_log_filename(
                    self.args.chip, self.args.interface, serial)
                print(f"Writing RTT log to: {self._log_filename}")

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
        emulators = logger.list_emulators()
        while not emulators:
            if logger._should_exit():
                print("\nCancelled.")
                logger._cleanup()
                sys.exit(0)
            print("Waiting for J-Link... (Ctrl+C to exit)")
            time.sleep(JLINK_WAIT_POLL_INTERVAL)
            emulators = logger.list_emulators()

        serial = select_jlink(emulators, args.serial)
        logger.run(serial)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        logger._cleanup()
