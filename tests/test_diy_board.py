"""Run: python -m pytest tests   (or: python tests/test_diy_board.py)"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from diy_board import SerialBrain, parse_line  # noqa: E402


def test_parse_line():
    assert parse_line(b"512\r") == 512
    assert parse_line(b"  7 ") == 7
    assert parse_line(b"") is None
    assert parse_line(b"boot garbage") is None


def test_simulator_streams_at_rate():
    b = SerialBrain("sim")
    b.start()
    try:
        time.sleep(2.0)
        n = len(b.buf)
    finally:
        b.stop()
    assert 400 < n < 600, n          # ~250 samples per second
    assert b.bad_lines == 0


if __name__ == "__main__":
    test_parse_line()
    test_simulator_streams_at_rate()
    print("all tests passed")
