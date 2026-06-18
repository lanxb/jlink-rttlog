"""Unit tests for jlink-rttlog pure functions.

These tests require no J-Link hardware — they exercise the argument
parsing, path sanitisation, ANSI stripping, and selection logic in
isolation.
"""

import os
import sys
import importlib.util
from unittest.mock import patch

import pytest

# The source file is named jlink-rttlog.py (hyphen — not a valid Python
# identifier), so we load it via importlib.
_MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'jlink-rttlog.py')
_spec = importlib.util.spec_from_file_location('jlink_rttlog', _MODULE_PATH)
jl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jl)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeEmulator:
    """Minimal emulator stub for select_jlink tests."""
    def __init__(self, serial):
        self.SerialNumber = serial


# ---------------------------------------------------------------------------
# _sanitize_path_component
# ---------------------------------------------------------------------------

class TestSanitizePathComponent:
    def test_normal(self):
        assert jl._sanitize_path_component("GD32F303VG") == "GD32F303VG"

    def test_path_traversal(self):
        # '/' and '\' replaced with '_' — no path separators escape through
        result = jl._sanitize_path_component("../../../etc")
        assert "/" not in result
        assert "\\" not in result

    def test_backslash(self):
        assert jl._sanitize_path_component("chip\\name") == "chip_name"

    def test_colon(self):
        assert jl._sanitize_path_component("C:evil") == "C_evil"

    def test_mixed_separators(self):
        assert jl._sanitize_path_component("a/b\\c:d") == "a_b_c_d"

    def test_int_input(self):
        assert jl._sanitize_path_component(12345) == "12345"


# ---------------------------------------------------------------------------
# _strip_ansi
# ---------------------------------------------------------------------------

class TestStripAnsi:
    def test_color_code(self):
        assert jl._strip_ansi("\x1b[31mRED\x1b[0m") == "RED"

    def test_clean_text_passthrough(self):
        assert jl._strip_ansi("Hello World") == "Hello World"

    def test_cursor_clear(self):
        assert jl._strip_ansi("\x1b[2J") == ""

    def test_mixed_content(self):
        text = "OK\x1b[31mERROR\x1b[0mOK"
        assert jl._strip_ansi(text) == "OKERROROK"

    def test_empty_string(self):
        assert jl._strip_ansi("") == ""

    def test_only_ansi(self):
        assert jl._strip_ansi("\x1b[1;32m\x1b[0m") == ""


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------

class TestParseArgs:
    def test_defaults(self):
        with patch.object(sys, 'argv', ['jlink-rttlog']):
            args = jl.parse_args()
            assert args.serial is None
            assert args.interface == 'swd'
            assert args.chip == 'GD32F303VG'
            assert args.speed == 4000
            assert args.threshold is None  # auto-detect on connect
            assert args.rtt_buffer == 0
            assert args.interval == 0.01

    def test_invalid_speed(self):
        with patch.object(sys, 'argv', ['jlink-rttlog', '--speed', '0']):
            with pytest.raises(SystemExit):
                jl.parse_args()

    def test_invalid_interval(self):
        with patch.object(sys, 'argv', ['jlink-rttlog', '--interval', '0']):
            with pytest.raises(SystemExit):
                jl.parse_args()

    def test_invalid_threshold(self):
        with patch.object(sys, 'argv', ['jlink-rttlog', '--threshold', '-1']):
            with pytest.raises(SystemExit):
                jl.parse_args()

    def test_invalid_rtt_buffer(self):
        with patch.object(sys, 'argv', ['jlink-rttlog', '--rtt-buffer', '-1']):
            with pytest.raises(SystemExit):
                jl.parse_args()

    def test_valid_custom(self):
        with patch.object(sys, 'argv', [
            'jlink-rttlog', '-s', '12345', '-c', 'STM32F407VG',
            '--speed', '8000', '--threshold', '1200'
        ]):
            args = jl.parse_args()
            assert args.serial == 12345
            assert args.chip == 'STM32F407VG'
            assert args.speed == 8000
            assert args.threshold == 1200


# ---------------------------------------------------------------------------
# select_jlink
# ---------------------------------------------------------------------------

class TestSelectJlink:
    def test_by_serial_match(self, capsys):
        emulators = [_FakeEmulator(111), _FakeEmulator(222), _FakeEmulator(333)]
        result = jl.select_jlink(emulators, 222)
        assert result == 222

    def test_auto_first_when_none(self, capsys):
        emulators = [_FakeEmulator(999)]
        result = jl.select_jlink(emulators, None)
        assert result == 999

    def test_not_found_raises_valueerror(self):
        """Specified serial not found → ValueError, no fallback."""
        emulators = [_FakeEmulator(100), _FakeEmulator(200)]
        with pytest.raises(ValueError, match='999'):
            jl.select_jlink(emulators, 999)


# ---------------------------------------------------------------------------
# make_log_filename
# ---------------------------------------------------------------------------

class TestMakeLogFilename:
    def test_sanitized_components(self):

        with patch.object(jl, 'LOG_DIR', '/fake/logs'):
            filename = jl.make_log_filename(
                chip="../../../evil",
                interface="swd",
                serial_number=12345,
            )
            # no path separators escape into the filename
            assert "/" not in os.path.basename(filename)
            assert "\\" not in os.path.basename(filename)
            # resolved path stays within LOG_DIR
            assert os.path.abspath(filename).startswith(
                os.path.abspath("/fake/logs"))
            # starts with LOG_DIR
            assert filename.startswith("/fake/logs")
            # contains expected parts
            basename = os.path.basename(filename)
            assert basename.startswith("rtt_")
            assert basename.endswith(".log")
            assert "swd" in basename
            assert "12345" in basename

    def test_normal_chip_name(self):

        with patch.object(jl, 'LOG_DIR', '/tmp'):
            filename = jl.make_log_filename("GD32F303VG", "jtag", 999)
            basename = os.path.basename(filename)
            assert "GD32F303VG" in basename
            assert "jtag" in basename
            assert "999" in basename
