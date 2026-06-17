import argparse
import pylink
import time
from datetime import datetime


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


def make_log_filename(args, serial_number):
    """Generate log filename: rtt_{chip}_{iface}_{serial}_{timestamp}.log"""
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return f'rtt_{args.chip}_{args.interface}_{serial_number}_{ts}.log'


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

            # 2. start RTT (MUST re-start after every reconnect)
            jlink.rtt_start()

            # 3. create a new log file on each reconnect
            log_filename = make_log_filename(args, serial)
            print(f"Writing RTT log to: {log_filename}")

            # 4. read loop
            while True:
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
                data = jlink.rtt_read(args.rtt_buffer, 1024)
                if data:
                    text = bytes(data).decode('utf-8', errors='replace')
                    print(text, end='', flush=True)
                    with open(log_filename, 'ab') as f:
                        f.write(bytes(data))

                time.sleep(args.interval)

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
    args = parse_args()

    emulators = list_emulators()
    if not emulators:
        exit(1)

    serial = select_jlink(emulators, args.serial)
    rtt_auto_reconnect(serial, args)
