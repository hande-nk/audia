"""
Audia Phase 0: "It's alive."

Streams EEG from BrainFlow (synthetic board by default, Muse 2 later),
computes band powers every second, turns them into an engagement score,
picks a music mode with hysteresis, and shows it all in a live dashboard.
Optional: a tiny synth whose tempo follows the mode (--sound).

Run:
    python audia_live.py              # live dashboard, synthetic EEG
    python audia_live.py --sound      # same, plus reactive audio
    python audia_live.py --headless   # terminal only, 25 seconds
    python audia_live.py --headless --script --seconds 65   # loop self-test
    python audia_live.py --serial sim       # simulated DIY board over serial
    python audia_live.py --serial /dev/cu.usbmodem101   # your own board

Keys in the dashboard window (TEST HARNESS, clearly fake signal):
    f  inject a 20 Hz beta wave   -> should push the score up (FOCUS)
    r  inject a 10 Hz alpha wave  -> should push the score down (CALM)
    n  stop injecting
"""

import argparse
import sys
import time
from collections import deque

import numpy as np
from brainflow.board_shim import BoardIds, BoardShim, BrainFlowInputParams
from brainflow.data_filter import DataFilter, DetrendOperations

# ----------------------------------------------------------------- config
WINDOW_SEC = 6          # seconds per estimate (BrainFlow needs ~1172 samples)
UPDATE_SEC = 1.0        # how often we recompute
BASELINE_SEC = 15       # first N seconds set the personal baseline
HISTORY_SEC = 90        # how much score history the plot shows
BANDS = ["delta", "theta", "alpha", "beta", "gamma"]

# Modes, with tempo targets. Hysteresis: you must cross ENTER to switch in,
# and drop past EXIT to leave. Stops songs flipping every few seconds.
MODES = {
    "CALM":    {"bpm": 70,  "color": "#7aa2ff"},
    "NEUTRAL": {"bpm": 90,  "color": "#9aa4b2"},
    "FOCUS":   {"bpm": 115, "color": "#3ef2b0"},
}
FOCUS_ENTER, FOCUS_EXIT = 65, 55
CALM_ENTER, CALM_EXIT = 35, 45
MIN_DWELL_SEC = 8       # minimum time in a mode before switching again
MIN_SPREAD = 0.15       # floor on baseline spread (log units). Without it, a very
                        # steady baseline turns tiny noise into fake mode swings.


# ----------------------------------------------------------- signal side
class Brain:
    """Wraps a BrainFlow board. Swap board_id to go from synthetic to Muse 2."""

    def __init__(self, board_id=BoardIds.SYNTHETIC_BOARD, mac=""):
        BoardShim.disable_board_logger()
        params = BrainFlowInputParams()
        params.mac_address = mac
        self.board_id = board_id
        self.board = BoardShim(board_id, params)
        self.fs = BoardShim.get_sampling_rate(board_id)
        # Muse 2 and synthetic both report several EEG channels; use first 4.
        self.eeg = BoardShim.get_eeg_channels(board_id)[:4]
        self.inject = None  # None, "beta", or "alpha"  (test harness only)

    def start(self):
        self.board.prepare_session()
        self.board.start_stream()

    def stop(self):
        try:
            self.board.stop_stream()
        finally:
            self.board.release_session()

    def window(self):
        """Last WINDOW_SEC of data, with optional injected test wave."""
        n = int(WINDOW_SEC * self.fs)
        data = self.board.get_current_board_data(n)
        if data.shape[1] < n:
            return None
        data = np.ascontiguousarray(data, dtype=np.float64)
        if self.inject:
            hz = 20.0 if self.inject == "beta" else 10.0
            t = np.arange(n) / self.fs
            for ch in self.eeg:
                amp = 3.0 * np.std(data[ch])
                data[ch] += amp * np.sin(2 * np.pi * hz * t)
        return data

    def band_powers(self, data):
        """Average relative band power across channels: delta..gamma."""
        for ch in self.eeg:
            DataFilter.detrend(data[ch], DetrendOperations.LINEAR.value)
        avg, _std = DataFilter.get_avg_band_powers(data, self.eeg, self.fs, True)
        return dict(zip(BANDS, avg))


def engagement_index(bp):
    """beta / (alpha + theta). Classic engagement index (Pope et al., 1995).
    We log it so ratios behave symmetrically around the baseline."""
    return float(np.log(bp["beta"] / (bp["alpha"] + bp["theta"] + 1e-9) + 1e-9))


class Scorer:
    """Turns the raw index into 0-100 relative to YOUR baseline.
    50 = your normal. This is a trend signal, not a mind reading."""

    def __init__(self):
        self.baseline = []
        self.mu, self.sd = None, None
        self.smooth = None

    def update(self, idx, elapsed):
        if elapsed < BASELINE_SEC:
            self.baseline.append(idx)
            return None
        if self.mu is None:
            b = np.array(self.baseline)
            self.mu, self.sd = float(b.mean()), max(float(b.std()), MIN_SPREAD)
        z = (idx - self.mu) / self.sd
        score = 50 + 50 * np.tanh(z / 2)          # squash to 0..100
        # exponential smoothing so the number does not jitter
        self.smooth = score if self.smooth is None else 0.7 * self.smooth + 0.3 * score
        return float(self.smooth)


class Controller:
    """Picks a music mode from the score, with hysteresis and minimum dwell."""

    def __init__(self):
        self.mode = "NEUTRAL"
        self.since = 0.0

    def update(self, score):
        if score is None:
            return self.mode, "calibrating to your baseline"
        now = time.time()
        if now - self.since < MIN_DWELL_SEC:
            return self.mode, f"holding {self.mode.lower()} (dwell)"
        new = self.mode
        if self.mode == "FOCUS" and score < FOCUS_EXIT:
            new = "NEUTRAL"
        elif self.mode == "CALM" and score > CALM_EXIT:
            new = "NEUTRAL"
        elif self.mode == "NEUTRAL":
            if score > FOCUS_ENTER:
                new = "FOCUS"
            elif score < CALM_ENTER:
                new = "CALM"
        why = f"score {score:.0f}"
        if new != self.mode:
            why = f"switched {self.mode.lower()} -> {new.lower()} at score {score:.0f}"
            self.mode, self.since = new, now
        return self.mode, why


# ------------------------------------------------------------ sound side
class Synth:
    """Soft pulse + drone. Tempo and pitch follow the mode. Optional."""

    def __init__(self):
        import sounddevice as sd  # imported here so the rest runs without it
        self.sd = sd
        self.sr = 44100
        self.bpm = MODES["NEUTRAL"]["bpm"]
        self.root = 220.0
        self.phase = 0
        self.stream = sd.OutputStream(samplerate=self.sr, channels=1,
                                      callback=self._cb, blocksize=1024)

    def set_mode(self, mode):
        self.bpm = MODES[mode]["bpm"]
        self.root = {"CALM": 196.0, "NEUTRAL": 220.0, "FOCUS": 261.6}[mode]

    def _cb(self, out, frames, _time, _status):
        t = (self.phase + np.arange(frames)) / self.sr
        beat = 60.0 / self.bpm
        pos = np.mod(t, beat)
        pulse = np.exp(-pos * 18) * np.sin(2 * np.pi * self.root * 2 * t) * 0.25
        drone = (np.sin(2 * np.pi * self.root * t)
                 + 0.5 * np.sin(2 * np.pi * self.root * 1.5 * t)) * 0.06
        out[:, 0] = (pulse + drone).astype(np.float32)
        self.phase += frames

    def start(self):
        self.stream.start()

    def stop(self):
        self.stream.stop()
        self.stream.close()


# --------------------------------------------------------------- runners
def source_label(brain):
    if getattr(brain, "label", None):
        return brain.label
    return "SYNTHETIC SIGNAL" if brain.board_id == BoardIds.SYNTHETIC_BOARD else "LIVE"


def run_headless(brain, seconds, synth=None, script=False):
    scorer, ctrl = Scorer(), Controller()
    t0 = time.time()
    # --script: beta from 20-35 s, alpha from 40-60 s, to prove the loop reacts
    plan = [(20, 35, "beta"), (40, 60, "alpha")] if script else []
    print(f"Audia | {source_label(brain)} | fs={brain.fs} Hz")
    print(" t   alpha  beta  theta  index  score  mode     why")
    while time.time() - t0 < seconds:
        time.sleep(UPDATE_SEC)
        now = time.time() - t0
        brain.inject = next((w for a, b, w in plan if a <= now < b), None)
        data = brain.window()
        if data is None:
            continue
        bp = brain.band_powers(data)
        idx = engagement_index(bp)
        el = time.time() - t0
        score = scorer.update(idx, el)
        mode, why = ctrl.update(score)
        if synth:
            synth.set_mode(mode)
        s = "  -- " if score is None else f"{score:5.1f}"
        inj = f"  [TEST: {brain.inject}]" if brain.inject else ""
        print(f"{el:3.0f}  {bp['alpha']:.3f} {bp['beta']:.3f} {bp['theta']:.3f}"
              f"  {idx:+.2f}  {s}  {mode:<8} {why}{inj}")


def run_dashboard(brain, synth=None):
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    BG, FG, MINT, DIM = "#0b0d10", "#e6e8eb", "#3ef2b0", "#2a2f36"
    plt.rcParams.update({"font.family": "monospace", "text.color": FG,
                         "axes.labelcolor": FG, "xtick.color": "#8a929c",
                         "ytick.color": "#8a929c", "axes.edgecolor": DIM})

    fig = plt.figure(figsize=(12, 7), facecolor=BG)
    fig.canvas.manager.set_window_title("Audia | live")
    gs = fig.add_gridspec(3, 3, height_ratios=[1.2, 1, 1.2], hspace=0.55, wspace=0.3)
    ax_wave = fig.add_subplot(gs[0, :])
    ax_band = fig.add_subplot(gs[1, 0])
    ax_now = fig.add_subplot(gs[1, 1:])
    ax_score = fig.add_subplot(gs[2, :])
    for ax in (ax_wave, ax_band, ax_now, ax_score):
        ax.set_facecolor(BG)

    title = fig.suptitle("", color=FG, fontsize=11, x=0.02, ha="left")
    scorer, ctrl = Scorer(), Controller()
    hist = deque(maxlen=int(HISTORY_SEC / UPDATE_SEC))
    t0 = time.time()

    def on_key(ev):
        brain.inject = {"f": "beta", "r": "alpha", "n": None}.get(ev.key, brain.inject)
    fig.canvas.mpl_connect("key_press_event", on_key)

    def frame(_):
        data = brain.window()
        if data is None:
            return
        el = time.time() - t0
        bp = brain.band_powers(data.copy())
        idx = engagement_index(bp)
        score = scorer.update(idx, el)
        mode, why = ctrl.update(score)
        if synth:
            synth.set_mode(mode)
        hist.append((el, score))
        color = MODES[mode]["color"]

        # 1. raw waves, last 4 s, 4 channels stacked
        ax_wave.clear(); ax_wave.set_facecolor(BG)
        t = np.arange(data.shape[1]) / brain.fs
        for i, ch in enumerate(brain.eeg):
            sig = data[ch] - np.mean(data[ch])
            sig = sig / (np.std(sig) + 1e-9)
            ax_wave.plot(t, sig + i * 5, lw=0.7, color=MINT if i % 2 == 0 else "#5fd3ff")
        ax_wave.set_yticks([]); ax_wave.set_xlabel("seconds")
        ax_wave.set_title(f"raw EEG, {len(brain.eeg)} channel(s), last {WINDOW_SEC} s", loc="left", fontsize=9, color="#8a929c")

        # 2. band powers
        ax_band.clear(); ax_band.set_facecolor(BG)
        vals = [bp[b] for b in BANDS]
        ax_band.bar(["δ", "θ", "α", "β", "γ"], vals,
                    color=[DIM, "#7aa2ff", "#7aa2ff", MINT, DIM])
        ax_band.set_ylim(0, max(0.6, max(vals) * 1.1))
        ax_band.set_title("relative band power", loc="left", fontsize=9, color="#8a929c")

        # 3. now playing card
        ax_now.clear(); ax_now.set_facecolor(BG); ax_now.axis("off")
        ax_now.text(0.0, 0.78, mode, fontsize=34, color=color, weight="bold")
        ax_now.text(0.0, 0.48, f"{MODES[mode]['bpm']} BPM target", fontsize=13, color=FG)
        ax_now.text(0.0, 0.25, f"why: {why}", fontsize=10, color="#8a929c")
        if hasattr(brain, "signal_ok"):
            ok = brain.signal_ok(data)
            ax_now.text(0.62, 0.78, "● signal OK" if ok else "● check contact",
                        fontsize=11, color=MINT if ok else "#ff8a5c")
        tag = f"TEST INJECTION: {brain.inject.upper()} WAVE" if brain.inject else ""
        ax_now.text(0.0, 0.03, tag, fontsize=10, color="#ff8a5c")

        # 4. score timeline with mode thresholds
        ax_score.clear(); ax_score.set_facecolor(BG)
        xs = [h[0] for h in hist if h[1] is not None]
        ys = [h[1] for h in hist if h[1] is not None]
        ax_score.axhspan(FOCUS_ENTER, 100, color=MINT, alpha=0.06)
        ax_score.axhspan(0, CALM_ENTER, color="#7aa2ff", alpha=0.06)
        ax_score.axhline(50, color=DIM, lw=0.8, ls="--")
        if xs:
            ax_score.plot(xs, ys, color=color, lw=2)
            ax_score.set_xlim(max(0, xs[-1] - HISTORY_SEC), max(HISTORY_SEC / 3, xs[-1] + 2))
        ax_score.set_ylim(0, 100)
        ax_score.set_title("engagement vs. your baseline (50 = normal)", loc="left",
                           fontsize=9, color="#8a929c")

        src = source_label(brain)
        cal = " | calibrating..." if score is None else ""
        title.set_text(f"AUDIA  ·  {src}  ·  {el:4.0f}s{cal}    keys: f=beta  r=alpha  n=off")

    _anim = FuncAnimation(fig, frame, interval=int(UPDATE_SEC * 1000), cache_frame_data=False)
    plt.show()


def main():
    ap = argparse.ArgumentParser(description="Audia Phase 0 live demo")
    ap.add_argument("--headless", action="store_true", help="terminal output only")
    ap.add_argument("--seconds", type=int, default=25, help="headless run length")
    ap.add_argument("--script", action="store_true",
                    help="headless: inject beta then alpha to test the loop")
    ap.add_argument("--sound", action="store_true", help="play reactive synth")
    ap.add_argument("--muse", action="store_true", help="use a real Muse 2 (Phase 2)")
    ap.add_argument("--mac", default="", help="Muse MAC address, optional")
    ap.add_argument("--serial", default="",
                    help="DIY board serial port, or 'sim' for the simulator")
    args = ap.parse_args()

    board = BoardIds.MUSE_2_BOARD if args.muse else BoardIds.SYNTHETIC_BOARD
    if args.serial:
        from diy_board import SerialBrain
        brain = SerialBrain(args.serial, window_sec=WINDOW_SEC)
    else:
        brain = Brain(board, args.mac)
    synth = None
    if args.sound:
        try:
            synth = Synth()
            synth.start()
        except Exception as e:  # no audio device, missing lib, etc.
            print(f"(sound off: {e})", file=sys.stderr)
            synth = None

    brain.start()
    try:
        if args.headless:
            run_headless(brain, args.seconds, synth, args.script)
        else:
            run_dashboard(brain, synth)
    except KeyboardInterrupt:
        pass
    finally:
        brain.stop()
        if synth:
            synth.stop()


if __name__ == "__main__":
    main()
