"""
Audia DIY board input.

Reads raw ADC samples from a microcontroller over USB serial (one integer
per line, fixed sample rate) and exposes the same interface as the BrainFlow
source in audia_live.py, so the whole pipeline runs unchanged on your own
hardware.

    python audia_live.py --serial sim                 # simulated board, no parts
    python audia_live.py --serial /dev/cu.usbmodem101 # your Arduino (Mac)

The simulator writes fake samples into pyserial's loop:// port and the reader
parses them back, so tonight you test the exact code path your board will use.

SAFETY: when electrodes touch your body, the laptop runs on battery with the
charger UNPLUGGED, and nothing mains-powered (monitor, USB hub, desk speaker)
is connected to the laptop. See HARDWARE.md.
"""

import threading
import time
from collections import deque

import numpy as np
import serial
from brainflow.data_filter import DataFilter, DetrendOperations

FS = 250              # must match SAMPLE_HZ in firmware/audia_adc/audia_adc.ino
BAUD = 115200
BANDS = ["delta", "theta", "alpha", "beta", "gamma"]


class SerialBrain:
    """One-channel EEG from a DIY front end (e.g. BioAmp EXG Pill + Arduino)."""

    def __init__(self, port, fs=FS, window_sec=6):
        self.port_name = port
        self.fs = fs
        self.window_sec = window_sec
        self.eeg = [0]                      # row 0 holds the only channel
        self.board_id = None
        self.label = "SIMULATED SERIAL" if port == "sim" else f"DIY BOARD {port}"
        self.inject = None                  # test harness, same as audia_live
        self.buf = deque(maxlen=fs * 30)
        self.bad_lines = 0
        self._stop = threading.Event()
        self._threads = []
        self.ser = None

    # ---------------------------------------------------------- lifecycle
    def start(self):
        if self.port_name == "sim":
            self.ser = serial.serial_for_url("loop://", timeout=0.1)
            t = threading.Thread(target=_simulate, args=(self.ser, self.fs, self._stop),
                                 daemon=True)
            t.start(); self._threads.append(t)
        else:
            self.ser = serial.Serial(self.port_name, BAUD, timeout=0.1)
            time.sleep(2)                   # Arduino resets when the port opens
            self.ser.reset_input_buffer()
        t = threading.Thread(target=self._read, daemon=True)
        t.start(); self._threads.append(t)

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1)
        if self.ser:
            self.ser.close()

    # --------------------------------------------------------------- read
    def _read(self):
        pending = b""
        while not self._stop.is_set():
            chunk = self.ser.read(4096)
            if not chunk:
                continue
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                v = parse_line(line)
                if v is None:
                    self.bad_lines += 1
                else:
                    self.buf.append(v)

    # ---------------------------------------------------- same API as Brain
    def window(self):
        n = int(self.window_sec * self.fs)
        if len(self.buf) < n:
            return None
        x = np.array(list(self.buf)[-n:], dtype=np.float64)
        x -= x.mean()                       # ADC counts centred on zero
        if self.inject:
            hz = 20.0 if self.inject == "beta" else 10.0
            t = np.arange(n) / self.fs
            x += 3.0 * np.std(x) * np.sin(2 * np.pi * hz * t)
        return np.ascontiguousarray(x.reshape(1, -1))

    def band_powers(self, data):
        DataFilter.detrend(data[0], DetrendOperations.LINEAR.value)
        # apply_filter=True: BrainFlow band-passes and notches mains hum
        avg, _ = DataFilter.get_avg_band_powers(data, self.eeg, self.fs, True)
        return dict(zip(BANDS, avg))

    def signal_ok(self, data):
        """Crude contact check: flat line or clipped ADC means bad contact."""
        x = data[0]
        return 1.0 < np.std(x) < 300.0


def parse_line(line: bytes):
    """One integer per line. Returns None for junk (boot noise, partial lines)."""
    try:
        return int(line.strip())
    except ValueError:
        return None


def _simulate(port, fs, stop):
    """Fake front end: 10 Hz alpha + 20 Hz beta + 60 Hz mains hum + noise,
    scaled like a 10-bit ADC sitting at mid-rail (~512 counts).
    Every 20 s it alternates 'eyes open' and 'eyes closed' (alpha x3)."""
    rng = np.random.default_rng(0)
    i, period = 0, 1.0 / fs
    next_t = time.perf_counter()
    while not stop.is_set():
        t = i / fs
        eyes_closed = int(t // 20) % 2 == 1
        alpha = (3.0 if eyes_closed else 1.0) * 6 * np.sin(2 * np.pi * 10 * t)
        beta = 4 * np.sin(2 * np.pi * 20 * t)
        hum = 8 * np.sin(2 * np.pi * 60 * t)
        v = 512 + alpha + beta + hum + rng.normal(0, 4)
        port.write(f"{int(round(v))}\n".encode())
        i += 1
        next_t += period
        sleep = next_t - time.perf_counter()
        if sleep > 0:
            time.sleep(sleep)
