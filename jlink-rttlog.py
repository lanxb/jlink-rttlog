import argparse
import ctypes
import msvcrt
import os
import sys
import pylink
import time
from datetime import datetime

_shutdown = False


def _should_exit():
    if _shutdown:
        return True
    if not msvcrt.kbhit():
        return False
    return msvcrt.getch() in (b'\x03', b'\x1a')


if sys.platform == 'win32':
    _HR = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)

    @_HR
    def _on_close(ctrl_type):
        global _shutdown
        _shutdown = True
        return True

    ctypes.windll.kernel32.SetConsoleCtrlHandler(_on_close, True)


def _cleanup():
    print("\nShutting down...")
    try:
        jlink.rtt_stop()
    except Exception:
        pass
    try:
        jlink.close()
    except Exception:
        pass


def parse_args():
    parser = argparse.ArgumentParser(
        description='J-Link RTT logger with auto-reconnect on power loss',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
examples:
  %(prog)s                                              # auto-select J-Link, default GD32F303VG
  %(prog)s -s 20630302 -c STM32F407VG                   # specify serial and chip
  %(prog)s -s 20630302 -c GD32F303VG --speed 8000       # 8MHz speed
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
    return parser.parse_args()


jlink = pylink.JLink()

# interface name -> pylink enum
INTERFACE_MAP = {
    'swd': pylink.enums.JLinkInterfaces.SWD,
    'jtag': pylink.enums.JLinkInterfaces.JTAG,
}


def list_emulators():
    """List all connected J-Link devices."""
    emulators = jlink.connected_emulators()
    if not emulators:
        print("[!] No J-Link devices detected")
        return []
    print(f"\nFound {len(emulators)} J-Link device(s):")
    for i, emu in enumerate(emulators):
        product = emu.acProduct.decode().strip('\x00')
        print(f"  [{i}] {product}  serial={emu.SerialNumber}  conn={emu.Connection}")
    return emulators


def select_jlink(emulators, target_serial):
    """Select J-Link by serial number, or auto-pick the first one."""
    if target_serial is not None:
        matched = [e for e in emulators if e.SerialNumber == target_serial]
        if matched:
            serial = matched[0].SerialNumber
            print(f"Selected J-Link (serial={serial})\n")
            return serial
        else:
            print(f"[!] J-Link serial={target_serial} not found, using first available")

    serial = emulators[0].SerialNumber
    print(f"Auto-selected J-Link (serial={serial})\n")
    return serial


if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOG_DIR = os.path.join(BASE_DIR, 'logs')


def make_log_filename(args, serial_number):
    """Generate log path: logs/rtt_{chip}_{iface}_{serial}_{timestamp}.log"""
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return os.path.join(LOG_DIR, f'rtt_{args.chip}_{args.interface}_{serial_number}_{ts}.log')


def rtt_auto_reconnect(serial, args):
    while True:
        try:
            # 1. ensure clean state then open J-Link
            if jlink.opened():
                try:
                    jlink.close()
                except Exception:
                    pass
                time.sleep(0.3)
            jlink.open(serial_no=serial)
            jlink.set_tif(INTERFACE_MAP[args.interface])
            jlink.connect(args.chip, speed=args.speed)
            print(f"Connected ({args.chip}, {args.interface.upper()}, {args.speed}kHz). Starting RTT...")

            # 2. start RTT and wait for control block detection
            jlink.rtt_start()
            for _ in range(30):  # up to 3 seconds
                if _should_exit():
                    return _cleanup()
                try:
                    if jlink.rtt_get_num_up_buffers() > 0:
                        print("RTT control block found, ready.")
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            else:
                print("RTT control block not found, will keep trying...")

            # 3. create a new log file on each reconnect
            log_filename = make_log_filename(args, serial)
            print(f"Writing RTT log to: {log_filename}")

            # 4. read loop
            while True:
                if _should_exit():
                    return _cleanup()

                # check target voltage
                try:
                    v = jlink.hardware_status.voltage
                except Exception:
                    v = 0  # treat read failure as power loss

                if v < args.threshold:
                    print(f"Power lost! (VTarget={v}mV) Waiting for power on...")
                    try:
                        jlink.rtt_stop()
                    except Exception:
                        pass
                    jlink.close()
                    time.sleep(0.3)  # let DLL release before reconnecting
                    break  # exit inner loop, outer loop will reconnect

                # read RTT data (non-blocking, returns empty list if no data)
                try:
                    data = jlink.rtt_read(args.rtt_buffer, 1024)
                except Exception:
                    data = []
                if data:
                    text = bytes(data).decode('utf-8', errors='replace')
                    print(text, end='', flush=True)
                    with open(log_filename, 'ab') as f:
                        f.write(bytes(data))

                time.sleep(args.interval)

        except KeyboardInterrupt:
            print()
            break
        except Exception as e:
            err = str(e)
            if 'already open' in err.lower():
                print("J-Link is in use by another instance, retrying in 2s...")
            else:
                print(f"Error: {e}, retrying in 2s...")
            try:
                jlink.rtt_stop()
            except Exception:
                pass
            if jlink.opened():
                try:
                    jlink.close()
                except Exception:
                    pass
            time.sleep(2)


if __name__ == "__main__":
    try:
        args = parse_args()

        emulators = list_emulators()
        if not emulators:
            exit(1)

        serial = select_jlink(emulators, args.serial)
        rtt_auto_reconnect(serial, args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        _cleanup()
