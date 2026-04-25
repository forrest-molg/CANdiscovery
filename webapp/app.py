#!/usr/bin/env python3
"""
CANdiscovery — CAN Bus Health Monitor using Analog Discovery 3 (AD3)

Dual-mode capture:
  - Digital:  Logic-analyser pins DIO0/DIO1 (CAN-H / CAN-L diff or single-ended TX)
              Samples at 4 Msps → 32 samples per 125 kbps bit.
              Decodes CAN frames and streams them to the UI exactly like CANapp.
  - Analog:   Oscilloscope channels CH1/CH2.
              Snapshot capture at 2 Msps for waveform health inspection.

Requires:
  - Digilent WaveForms runtime (libdwf.so) installed on the system.
  - pydwf Python package  (pip install pydwf)

Run: python webapp/app.py
Open: http://<raspberry-pi-ip>:5001
"""

import json
import os
import signal
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = "candiscovery-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Paths ──────────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).resolve().parent.parent / "CANlogs"
LOG_DIR.mkdir(exist_ok=True)

# ── libdwf availability check ──────────────────────────────────────────────────
_DWF_AVAILABLE = False
_DWF_ERROR     = ""
try:
    from pydwf import DwfLibrary, DwfState, DwfAcquisitionMode, DwfTriggerSource
    from pydwf import PyDwfError
    from pydwf.utilities import openDwfDevice
    from pydwf.core.auxiliary.enum_types import (
        DwfAnalogInFilter, DwfAnalogInTriggerType, DwfTriggerSlope,
        DwfDigitalInClockSource
    )
    _dwf = DwfLibrary()
    _DWF_AVAILABLE = True
except Exception as e:
    _DWF_ERROR = str(e)

# ── Constants for 125 kbps CAN ─────────────────────────────────────────────────
CAN_BITRATE          = 125_000          # bits / second
DIGITAL_SAMPLE_RATE  = 4_000_000        # 4 Msps  →  32 samples / bit
ANALOG_SAMPLE_RATE   = 2_000_000        # 2 Msps  →  16 samples / bit
ANALOG_CAPTURE_SEC   = 0.05             # 50 ms snapshot  (6250 CAN bits)
ANALOG_BUFFER_SIZE   = int(ANALOG_SAMPLE_RATE * ANALOG_CAPTURE_SEC)  # 100 000 samples
DIGITAL_BUFFER_SIZE  = int(DIGITAL_SAMPLE_RATE * 0.1)               # 100 ms  ring

# Minimum samples to look for in one digital decode pass
# 10 bit-times = 1 CAN character; we need at least 1 full frame (≈130 bits)
MIN_DECODE_BITS      = 150
MIN_DECODE_SAMPLES   = MIN_DECODE_BITS * (DIGITAL_SAMPLE_RATE // CAN_BITRATE)

# ── ISO 11898-2 specifications for 125 kbps CAN ───────────────────────────────
CAN_BIT_TIME_US      = 1e6 / CAN_BITRATE          # 8.000 µs per bit
CAN_RISE_FALL_MAX_US  = 0.3 * CAN_BIT_TIME_US     # 2.400 µs max rise / fall (ISO 11898)
CAN_DOM_DIFF_MIN_V    = 1.5                         # V  differential dominant lower bound
CAN_DOM_DIFF_MAX_V    = 3.0                         # V  differential dominant upper bound
CAN_REC_DIFF_MAX_V    = 0.5                         # V  differential recessive upper bound
CAN_DOM_H_MIN_V       = 2.75                        # V  CAN-H dominant lower bound
CAN_DOM_H_MAX_V       = 4.5                         # V  CAN-H dominant upper bound
CAN_DOM_L_MAX_V       = 2.25                        # V  CAN-L dominant upper bound
CAN_DOM_L_MIN_V       = 0.5                         # V  CAN-L dominant lower bound
CAN_REC_H_MIN_V       = 2.0                         # V  CAN-H recessive lower bound
CAN_REC_H_MAX_V       = 3.0                         # V  CAN-H recessive upper bound
CAN_REC_L_MIN_V       = 2.0                         # V  CAN-L recessive lower bound
CAN_REC_L_MAX_V       = 3.0                         # V  CAN-L recessive upper bound
CAN_NOISE_MAX_MV      = 50.0                        # mV RMS noise limit in recessive phase

# Health-check capture parameters
HEALTH_SAMPLE_RATE   = 4_000_000        # 4 Msps  →  0.25 µs / sample
HEALTH_N_SAMPLES     = 16_384          # ~4.096 ms window
HEALTH_CAPTURE_SEC   = HEALTH_N_SAMPLES / HEALTH_SAMPLE_RATE

# ── Session state ──────────────────────────────────────────────────────────────
_device_index: int | None = None
_device_info: dict        = {}

_digital_thread: threading.Thread | None = None
_digital_stop   = threading.Event()
_digital_lock   = threading.Lock()

_analog_lock    = threading.Lock()

_FRAME_BUF_SIZE = 500
_frame_buf: deque = deque(maxlen=_FRAME_BUF_SIZE)
_replay_lock    = threading.Lock()
_last_status    = {"msg": "Disconnected", "running": False, "error": False}

# ── Logging (mirrors CANapp) ───────────────────────────────────────────────────
_log_lock       = threading.Lock()
_log_fh         = None
_log_active     = False
_log_path: Path | None = None


def _log_open():
    global _log_fh, _log_active, _log_path
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOG_DIR / f"candiscovery_{ts}.txt"
    fh   = open(path, "w", encoding="utf-8", errors="replace")
    fh.write(f"=== CANdiscovery Log  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    fh.write(f"Bitrate: {CAN_BITRATE} bps  |  Digital sample rate: {DIGITAL_SAMPLE_RATE/1e6:.1f} Msps\n")
    fh.write("=" * 70 + "\n\n")
    fh.flush()
    with _log_lock:
        _log_fh     = fh
        _log_active = True
        _log_path   = path
    socketio.emit("log_status", {"active": True, "filename": path.name,
                                  "msg": f"Logging to {path}"})


def _log_close():
    global _log_fh, _log_active, _log_path
    name = ""
    with _log_lock:
        if _log_fh:
            try:
                _log_fh.write(f"\n=== Log Closed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
                _log_fh.close()
            except Exception:
                pass
            _log_fh     = None
            _log_active = False
            name = _log_path.name if _log_path else ""
            _log_path   = None
    socketio.emit("log_status", {"active": False, "filename": None,
                                  "msg": f"Saved: {name}" if name else "Not logging"})


def _log_write(line: str):
    with _log_lock:
        if _log_active and _log_fh:
            try:
                _log_fh.write(line + "\n")
                _log_fh.flush()
            except Exception:
                pass


# ── AD3 device scanner ─────────────────────────────────────────────────────────
def scan_ad3_devices() -> list[dict]:
    """Return all connected AD3 / Analog Discovery devices."""
    if not _DWF_AVAILABLE:
        return []
    devices = []
    try:
        count = _dwf.deviceEnum.enumerateDevices()
        for i in range(count):
            try:
                name = _dwf.deviceEnum.deviceName(i)
                sn   = _dwf.deviceEnum.serialNumber(i)
                user = _dwf.deviceEnum.userName(i)
            except Exception:
                name, sn, user = f"Device {i}", "unknown", ""
            devices.append({
                "index":  i,
                "name":   name,
                "serial": str(sn),
                "user":   user,
                "label":  f"[{i}] {name}  (S/N: {sn})",
                "channels": {"analog": 2, "digital": 16},
            })
    except Exception:
        pass
    return devices


# ──────────────────────────────────────────────────────────────────────────────
# CAN bus health metrics (ISO 11898-2 / 125 kbps)
# ──────────────────────────────────────────────────────────────────────────────
def _compute_health_metrics(ch1: list, ch2: list, sample_rate: int) -> dict:
    """
    Derive ISO 11898-2 CAN bus health metrics from raw ADC voltage samples.

    ch1  : CAN-H voltages (list of floats)
    ch2  : CAN-L voltages (list of floats, same length as ch1)
    sample_rate : samples per second
    """
    n = len(ch1)
    if n < 100:
        return {"error": "Insufficient samples (<100)"}

    t_us = 1e6 / sample_rate          # µs per sample
    has_ch2 = len(ch2) == n and n > 0

    # Differential signal (CAN-H minus CAN-L)
    if has_ch2:
        diff = [h - l for h, l in zip(ch1, ch2)]
    else:
        # Approximate from single-ended CH1 referenced to 2.5 V common mode
        diff = [(v - 2.5) * 2 for v in ch1]

    diff_min, diff_max = min(diff), max(diff)
    if diff_max - diff_min < 0.3:
        if diff_max < 0.1:
            return {"error": "Bus appears idle (all recessive) — connect to an active CAN bus"}
        return {"error": "No dominant bits detected — check bus activity and connections"}

    # Robust level estimation using 20th / 80th percentiles
    sorted_d = sorted(diff)
    p20 = sorted_d[n // 5]
    p80 = sorted_d[4 * n // 5]
    v_threshold = (p20 + p80) / 2

    dom_idx = [i for i, v in enumerate(diff) if v > v_threshold]
    rec_idx = [i for i, v in enumerate(diff) if v <= v_threshold]

    if not dom_idx or not rec_idx:
        return {"error": "Cannot separate dominant / recessive phases"}

    def _mean(lst):   return sum(lst) / len(lst) if lst else 0.0
    def _rnd(v, d=4): return round(v, d) if v is not None else None

    v_dom_mean = _mean([diff[i] for i in dom_idx])
    v_rec_mean = _mean([diff[i] for i in rec_idx])
    v_dom_peak = max(diff[i] for i in dom_idx)
    v_rec_min  = min(diff[i] for i in rec_idx)
    v_swing    = v_dom_mean - v_rec_mean

    if v_swing < 0.3:
        return {"error": "Differential swing too small — check CAN-H / CAN-L wiring"}

    # 10 % / 90 % voltage levels for rise / fall measurement
    v_10 = v_rec_mean + 0.10 * v_swing
    v_90 = v_rec_mean + 0.90 * v_swing

    # ── Edge detection with sub-sample interpolation ───────────────────────────
    rise_edges: list[float] = []
    fall_edges: list[float] = []
    for i in range(n - 1):
        d0, d1 = diff[i], diff[i + 1]
        if d0 <= v_threshold < d1:
            frac = (v_threshold - d0) / (d1 - d0)
            rise_edges.append(i + frac)
        elif d0 > v_threshold >= d1:
            frac = (v_threshold - d0) / (d1 - d0)   # frac is negative, gives crossing point
            rise_edges.append(i + frac) if False else fall_edges.append(i + abs(frac) if d1!=d0 else i)
            # redo cleanly:

    # Redo edge detection cleanly
    rise_edges.clear()
    fall_edges.clear()
    for i in range(n - 1):
        d0, d1 = diff[i], diff[i + 1]
        if d0 <= v_threshold < d1:          # rising edge
            frac = (v_threshold - d0) / (d1 - d0) if d1 != d0 else 0.0
            rise_edges.append(i + frac)
        elif d0 > v_threshold >= d1:        # falling edge
            frac = (v_threshold - d0) / (d1 - d0) if d1 != d0 else 0.0
            fall_edges.append(i + frac)

    # ── Rise times (10 %→90 % on differential rising edges) ─────────────────
    def _measure_rise(edge_pos: float) -> float | None:
        i0 = int(edge_pos)
        # Search backward for 10 % crossing
        t10 = None
        for j in range(i0, max(0, i0 - 160), -1):
            if j + 1 < n and diff[j] <= v_10 < diff[j + 1]:
                frac = (v_10 - diff[j]) / (diff[j + 1] - diff[j])
                t10 = j + frac
                break
        if t10 is None:
            return None
        # Search forward for 90 % crossing
        for j in range(i0, min(n - 1, i0 + 160)):
            if diff[j] < v_90 <= diff[j + 1]:
                frac = (v_90 - diff[j]) / (diff[j + 1] - diff[j])
                t90 = j + frac
                rt = (t90 - t10) * t_us
                return rt if 0 < rt < 20 else None
        return None

    def _measure_fall(edge_pos: float) -> float | None:
        i0 = int(edge_pos)
        # Search backward for 90 % crossing
        t90 = None
        for j in range(i0, max(0, i0 - 160), -1):
            if j + 1 < n and diff[j] >= v_90 > diff[j + 1]:
                frac = (v_90 - diff[j]) / (diff[j + 1] - diff[j])
                t90 = j + frac
                break
        if t90 is None:
            return None
        # Search forward for 10 % crossing
        for j in range(i0, min(n - 1, i0 + 160)):
            if diff[j] > v_10 >= diff[j + 1]:
                frac = (v_10 - diff[j]) / (diff[j + 1] - diff[j])
                t10 = j + frac
                ft = (t10 - t90) * t_us
                return ft if 0 < ft < 20 else None
        return None

    rise_times = [rt for e in rise_edges[:30] for rt in [_measure_rise(e)]  if rt]
    fall_times = [ft for e in fall_edges[:30] for ft in [_measure_fall(e)] if ft]

    # ── Bit period from edge-to-edge intervals ────────────────────────────────
    bit_period_us = None
    all_edges = sorted(rise_edges + fall_edges)
    if len(all_edges) >= 4:
        intervals = [(all_edges[j + 1] - all_edges[j]) * t_us
                     for j in range(len(all_edges) - 1)]
        exp = CAN_BIT_TIME_US
        # Candidate bit periods: intervals close to 1–8 bit times
        candidates = [x for x in intervals
                      if abs(x - exp) < exp * 0.35 or
                         abs(x / 2 - exp) < exp * 0.35 or
                         abs(x / 3 - exp) < exp * 0.35]
        measured = []
        for x in candidates:
            for div in range(1, 9):
                if abs(x / div - exp) < exp * 0.35:
                    measured.append(x / div)
                    break
        if measured:
            bit_period_us = round(_mean(measured), 3)

    # ── Per-channel voltage stats ──────────────────────────────────────────────
    ch1_dom = [ch1[i] for i in dom_idx]
    ch1_rec = [ch1[i] for i in rec_idx]
    ch2_dom = [ch2[i] for i in dom_idx] if has_ch2 else []
    ch2_rec = [ch2[i] for i in rec_idx] if has_ch2 else []

    ch1_dom_peak = round(max(ch1_dom), 4) if ch1_dom else None
    ch1_dom_mean = round(_mean(ch1_dom), 4) if ch1_dom else None
    ch2_dom_min  = round(min(ch2_dom), 4) if ch2_dom else None
    ch2_dom_mean = round(_mean(ch2_dom), 4) if ch2_dom else None
    ch1_rec_mean = round(_mean(ch1_rec), 4) if ch1_rec else None
    ch2_rec_mean = round(_mean(ch2_rec), 4) if ch2_rec else None

    # ── Overshoot / undershoot ─────────────────────────────────────────────────
    os_pct = round(max(0.0, (v_dom_peak - v_dom_mean) / v_swing * 100), 1)
    us_pct = round(max(0.0, (v_rec_mean - v_rec_min) / v_swing * 100), 1)

    # ── Noise RMS in recessive regions on differential signal ─────────────────
    noise_mv = None
    if len(rec_idx) >= 20:
        rec_vals = [diff[i] for i in rec_idx]
        m = _mean(rec_vals)
        variance = _mean([(v - m) ** 2 for v in rec_vals])
        noise_mv = round((variance ** 0.5) * 1000, 1)

    # ── CAN frame decode from analog (differential → digital → CANDecoder) ────
    decoded_frame = None
    n_decoded = 0
    if v_swing >= 0.5:
        try:
            # CAN logic: dominant = diff HIGH → CAN bit 0; recessive = diff LOW → CAN bit 1
            can_bits = [0 if v > v_threshold else 1 for v in diff]
            # CANDecoder expects DIGITAL_SAMPLE_RATE; resample if needed
            if sample_rate != DIGITAL_SAMPLE_RATE:
                ratio = DIGITAL_SAMPLE_RATE / sample_rate
                new_len = int(len(can_bits) * ratio)
                can_bits = [can_bits[min(len(can_bits) - 1, int(i / ratio))]
                            for i in range(new_len)]
            dec = CANDecoder()
            dec.feed(can_bits)
            frames = dec.pop_frames()
            n_decoded = len(frames)
            decoded_frame = frames[0] if frames else None
        except Exception:
            pass

    # ── Pass / fail vs ISO 11898-2 ────────────────────────────────────────────
    avg_rise = round(_mean(rise_times), 3) if rise_times else None
    avg_fall = round(_mean(fall_times), 3) if fall_times else None

    issues: list[str] = []
    if avg_rise is not None and avg_rise > CAN_RISE_FALL_MAX_US:
        issues.append(f"Rise time {avg_rise:.2f} µs > spec ≤{CAN_RISE_FALL_MAX_US:.1f} µs")
    if avg_fall is not None and avg_fall > CAN_RISE_FALL_MAX_US:
        issues.append(f"Fall time {avg_fall:.2f} µs > spec ≤{CAN_RISE_FALL_MAX_US:.1f} µs")
    if bit_period_us is not None:
        err_pct = abs(bit_period_us - CAN_BIT_TIME_US) / CAN_BIT_TIME_US * 100
        if err_pct > 1.25:
            issues.append(
                f"Bit period {bit_period_us:.2f} µs — error {err_pct:.1f} % (spec ≤1.25 %)")
    if v_dom_peak < CAN_DOM_DIFF_MIN_V:
        issues.append(
            f"Diff. dominant {v_dom_peak:.2f} V < spec ≥{CAN_DOM_DIFF_MIN_V:.1f} V")
    if v_dom_peak > CAN_DOM_DIFF_MAX_V:
        issues.append(
            f"Diff. dominant {v_dom_peak:.2f} V > spec ≤{CAN_DOM_DIFF_MAX_V:.1f} V")
    if _rnd(v_rec_mean) > CAN_REC_DIFF_MAX_V:
        issues.append(
            f"Diff. recessive {v_rec_mean:.2f} V > spec ≤{CAN_REC_DIFF_MAX_V:.1f} V")
    if ch1_dom_peak is not None and ch1_dom_peak < CAN_DOM_H_MIN_V:
        issues.append(
            f"CAN-H dominant {ch1_dom_peak:.2f} V < spec ≥{CAN_DOM_H_MIN_V:.2f} V")
    if ch2_dom_min is not None and ch2_dom_min > CAN_DOM_L_MAX_V:
        issues.append(
            f"CAN-L dominant {ch2_dom_min:.2f} V > spec ≤{CAN_DOM_L_MAX_V:.2f} V")
    if ch1_rec_mean is not None and not (CAN_REC_H_MIN_V <= ch1_rec_mean <= CAN_REC_H_MAX_V):
        issues.append(
            f"CAN-H recessive {ch1_rec_mean:.2f} V outside spec "
            f"[{CAN_REC_H_MIN_V:.1f}–{CAN_REC_H_MAX_V:.1f}] V")
    if noise_mv is not None and noise_mv > CAN_NOISE_MAX_MV:
        issues.append(f"Noise {noise_mv:.0f} mV > spec ≤{CAN_NOISE_MAX_MV:.0f} mV")

    overall = "PASS" if not issues else "FAIL"

    return {
        "avg_rise_us":      avg_rise,
        "avg_fall_us":      avg_fall,
        "all_rise_us":      [round(x, 3) for x in rise_times[:12]],
        "all_fall_us":      [round(x, 3) for x in fall_times[:12]],
        "bit_period_us":    bit_period_us,
        "edge_count":       len(rise_edges) + len(fall_edges),
        "n_decoded":        n_decoded,
        "decoded_frame":    decoded_frame,
        "ch1_dom_peak_v":   ch1_dom_peak,
        "ch1_dom_mean_v":   ch1_dom_mean,
        "ch2_dom_min_v":    ch2_dom_min,
        "ch2_dom_mean_v":   ch2_dom_mean,
        "diff_dom_peak_v":  _rnd(v_dom_peak),
        "diff_dom_mean_v":  _rnd(v_dom_mean),
        "diff_rec_mean_v":  _rnd(v_rec_mean),
        "ch1_rec_mean_v":   ch1_rec_mean,
        "ch2_rec_mean_v":   ch2_rec_mean,
        "overshoot_pct":    os_pct,
        "undershoot_pct":   us_pct,
        "noise_rms_mv":     noise_mv,
        "overall":          overall,
        "issues":           issues,
        "spec": {
            "bitrate":            CAN_BITRATE,
            "bit_period_us":      CAN_BIT_TIME_US,
            "rise_fall_max_us":   CAN_RISE_FALL_MAX_US,
            "dom_diff_min_v":     CAN_DOM_DIFF_MIN_V,
            "dom_diff_max_v":     CAN_DOM_DIFF_MAX_V,
            "rec_diff_max_v":     CAN_REC_DIFF_MAX_V,
            "dom_h_min_v":        CAN_DOM_H_MIN_V,
            "dom_l_max_v":        CAN_DOM_L_MAX_V,
            "noise_max_mv":       CAN_NOISE_MAX_MV,
        },
    }


def _do_analog_health_check(dev_index: int) -> dict:
    """
    Capture 16384 samples at 4 MHz from AD3 CH1 + CH2.

    Root cause (proven across all attempts):
      ANY USB round-trip during active ADC DMA stalls the AD3 FPGA on RPi4.
      This includes status(True), status(False), AND triggerPC() — every USB
      transaction freezes the ADC regardless of mode or trigger source.

    Strategy: single-shot sleep-then-read
      - TriggerSource.None_ = free-run: device fires immediately at Armed,
        no USB trigger command required at all.
      - triggerPosition=0: trigger at center, Prefill=2ms, total capture=4ms.
      - Sleep 1 s with ZERO USB activity.  ADC Done for ~996 ms.
      - ONE status(True) call transfers the completed SRAM buffer.
      - Two statusData() calls read both channels from the host buffer.

    This was never tried before — all previous attempts polled in a loop which
    stalled the ADC on the first or second call.
    """
    import numpy as np

    def dbg(msg):
        print(f"[HEALTH-DBG] {msg}", flush=True)
        socketio.emit("health_check_status", {"msg": f"[DBG] {msg}", "busy": True})

    if not _DWF_AVAILABLE:
        return {"ok": False, "error": "libdwf not available"}
    try:
        dbg("Opening device...")
        with _dwf.deviceControl.open(dev_index) as device:
            scope = device.analogIn
            scope.reset()

            record_n  = HEALTH_N_SAMPLES               # 16 384
            capture_s = record_n / HEALTH_SAMPLE_RATE  # 4.096 ms

            for ch in range(2):
                scope.channelEnableSet(ch, True)
                scope.channelRangeSet(ch, 5.0)
                scope.channelOffsetSet(ch, 2.5)
                scope.channelFilterSet(ch, DwfAnalogInFilter.Decimate)

            scope.acquisitionModeSet(DwfAcquisitionMode.Single)
            scope.frequencySet(HEALTH_SAMPLE_RATE)
            scope.bufferSizeSet(record_n)
            # TriggerSource.None_ alone = device waits in Prefill forever.
            # triggerAutoTimeoutSet fires the trigger automatically after ~84ms
            # (firmware minimum) with zero USB interaction during acquisition.
            scope.triggerSourceSet(DwfTriggerSource.None_)
            scope.triggerPositionSet(0.0)           # trigger at center
            scope.triggerAutoTimeoutSet(0.001)      # snaps to ~84ms on AD3

            actual_freq    = scope.frequencyGet()
            actual_buf     = scope.bufferSizeGet()
            actual_timeout = scope.triggerAutoTimeoutGet()
            dbg(f"Single/autoTimeout: freq={actual_freq:.0f} Hz  buf={actual_buf}  "
                f"autoTimeout={actual_timeout*1000:.1f} ms")

            # Start: Prefill(2ms) → Armed → autoTimeout(84ms) → Triggered(2ms) → Done
            # Total acquisition = ~88ms. Sleep 500ms — Done for ~412ms before USB.
            scope.configure(False, True)
            dbg("Sleeping 500 ms (ADC hands-off)...")
            time.sleep(0.500)

            # Single status(True) — ADC has been Done for ~412 ms.
            state = scope.status(True)
            dbg(f"After sleep: state={state.name}")

            if state != DwfState.Done:
                return {"ok": False, "error":
                        f"Acquisition not Done after 500 ms: state={state}"}

            n_valid = scope.statusSamplesValid()
            dbg(f"n_valid={n_valid}/{record_n}")

            if n_valid < record_n:
                return {"ok": False, "error":
                        f"Only {n_valid}/{record_n} valid samples"}

            ch1_raw = scope.statusData(0, n_valid)[:record_n]
            ch2_raw = scope.statusData(1, n_valid)[:record_n]
            dbg(f"CH1 mean={ch1_raw.mean():.3f} V  CH2 mean={ch2_raw.mean():.3f} V")

            ch1 = [round(float(v), 5) for v in ch1_raw]
            ch2 = [round(float(v), 5) for v in ch2_raw]

            step = max(1, len(ch1) // 4000)
            t_step_us = step * (1e6 / actual_freq)
            wf_t   = [round(i * t_step_us, 3)     for i in range(len(ch1[::step]))]
            wf_ch1 = [round(float(v), 4)           for v in ch1[::step]]
            wf_ch2 = [round(float(v), 4)           for v in ch2[::step]]
            wf_diff= [round(float(ch1[i*step] - ch2[i*step]), 4) for i in range(len(wf_t))]

            metrics = _compute_health_metrics(ch1, ch2, actual_freq)

            return {
                "ok":     True,
                "waveform": {
                    "time_us": wf_t,
                    "ch1":     wf_ch1,
                    "ch2":     wf_ch2,
                    "diff":    wf_diff,
                },
                "metrics":     metrics,
                "sample_rate": actual_freq,
                "n_samples":   record_n,
            }

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ──────────────────────────────────────────────────────────────────────────────
# CAN decoder (bit-bang from logic samples)
# ──────────────────────────────────────────────────────────────────────────────
class CANDecoder:
    """
    Decodes a stream of raw digital samples (0/1 per sample) captured from a
    single-ended CAN-TX or dominant-low differential signal.

    Protocol:
      - Idle = high (recessive).  SOF = first dominant (0) bit.
      - Standard CAN frame (no FD): 11-bit ID, RTR, IDE=0, r0, 4-bit DLC,
        0-8 bytes data, 15-bit CRC, 1-bit CRCdelim, 1-bit ACK, 1-bit ACKdelim,
        7-bit EOF. No bit-stuffing in CRC/EOF.
    """

    SAMPLES_PER_BIT = DIGITAL_SAMPLE_RATE // CAN_BITRATE  # 32

    def __init__(self):
        self._buf: list[int] = []        # unprocessed samples
        self._frames: list[dict] = []    # decoded frames waiting to be emitted

    def feed(self, samples: list[int]):
        self._buf.extend(samples)
        self._decode_all()

    def pop_frames(self) -> list[dict]:
        out = self._frames[:]
        self._frames.clear()
        return out

    # ── internal ──────────────────────────────────────────────────────────────

    def _read_bit(self, pos: int) -> int | None:
        """Sample the bit centred at `pos` using majority vote ±4 samples."""
        spb = self.SAMPLES_PER_BIT
        sample_pos = pos + spb // 2
        lo = max(0, sample_pos - 4)
        hi = min(len(self._buf), sample_pos + 5)
        chunk = self._buf[lo:hi]
        if not chunk:
            return None
        return 1 if sum(chunk) > len(chunk) // 2 else 0

    def _decode_all(self):
        spb  = self.SAMPLES_PER_BIT
        buf  = self._buf
        pos  = 0
        while pos < len(buf):
            # Wait for SOF: transition from 1→0
            if buf[pos] != 0:
                pos += 1
                continue
            # Make sure we have enough buffer for a minimal frame
            # Min frame = SOF(1)+ID(11)+ctrl(6)+data(0)+crc(16)+eof(10) = ~44 bits
            if pos + 60 * spb > len(buf):
                break  # wait for more samples

            frame_start = pos
            bits: list[int] = []
            stuff_count = 0       # consecutive same-level count
            stuff_errors = 0
            raw_pos = pos         # decode loop position (no-stuffing view)
            sample_pos = pos      # actual buffer cursor

            def next_bit():
                nonlocal sample_pos, stuff_count, stuff_errors
                b = self._read_bit(sample_pos)
                if b is None:
                    return None
                sample_pos += spb

                # Bit stuffing: after 5 consecutive same bits, next is stuff
                if len(bits) > 0 and stuff_count >= 5:
                    expected_stuff = 1 - bits[-1]
                    if b != expected_stuff:
                        stuff_errors += 1
                    stuff_count = 1 if b == (1 - bits[-1] if bits else 0) else 1
                    return next_bit()   # recurse once to get real bit

                if bits and b == bits[-1]:
                    stuff_count += 1
                else:
                    stuff_count = 1
                return b

            try:
                # SOF (already dominant / 0)
                b = self._read_bit(sample_pos)
                if b != 0:
                    pos += 1
                    continue
                sample_pos += spb
                bits.append(0)
                stuff_count = 1

                # 11-bit ID
                for _ in range(11):
                    b = next_bit()
                    if b is None: raise ValueError("truncated")
                    bits.append(b)

                # RTR
                rtr = next_bit()
                if rtr is None: raise ValueError("truncated")
                bits.append(rtr)

                # IDE (must be 0 for standard frame)
                ide = next_bit()
                if ide is None: raise ValueError("truncated")
                if ide != 0:
                    # Extended frame (29-bit ID) — consume 18 more ID bits
                    bits.append(ide)
                    for _ in range(18 + 1 + 1):  # 18 ext bits + SRR-like + r1
                        b = next_bit()
                        if b is None: raise ValueError("truncated")
                        bits.append(b)
                bits.append(ide)

                # r0 (reserved)
                r0 = next_bit()
                if r0 is None: raise ValueError("truncated")
                bits.append(r0)

                # DLC (4 bits)
                dlc_bits = []
                for _ in range(4):
                    b = next_bit()
                    if b is None: raise ValueError("truncated")
                    dlc_bits.append(b)
                    bits.append(b)
                dlc = int("".join(str(b) for b in dlc_bits), 2)
                dlc = min(dlc, 8)

                # Data (dlc bytes)
                data_bytes: list[int] = []
                for _ in range(dlc):
                    byte_bits = []
                    for _ in range(8):
                        b = next_bit()
                        if b is None: raise ValueError("truncated")
                        byte_bits.append(b)
                        bits.append(b)
                    data_bytes.append(int("".join(str(x) for x in byte_bits), 2))

                # 15-bit CRC (no stuffing inside CRC for simplicity — skip validation)
                for _ in range(15):
                    b = next_bit()
                    if b is None: raise ValueError("truncated")
                    bits.append(b)

                # CRC delimiter (should be 1)
                crc_del = self._read_bit(sample_pos)
                sample_pos += spb

                # ACK slot (1) + ACK delimiter (1)
                sample_pos += spb * 2

                # EOF (7 recessive bits)
                for _ in range(7):
                    b = self._read_bit(sample_pos)
                    sample_pos += spb

                if stuff_errors > 2:
                    pos += 1
                    continue

                # Build ID
                id_bits = bits[1:12]
                arb_id = int("".join(str(b) for b in id_bits), 2)
                is_ext = (ide == 1)

                if is_ext:
                    ext_bits = bits[13:31]
                    arb_id = (arb_id << 18) | int("".join(str(b) for b in ext_bits), 2)
                    id_str = f"0x{arb_id:08X}"
                else:
                    id_str = f"0x{arb_id:03X}"

                ts_now = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                hex_bytes = [f"{b:02X}" for b in data_bytes]
                decoded   = "".join(chr(b) if 32 <= b <= 126 else "·" for b in data_bytes)

                frame = {
                    "ts":  ts_now,
                    "id":  id_str,
                    "ext": is_ext,
                    "dlc": dlc,
                    "hex": hex_bytes,
                    "text": decoded,
                }
                self._frames.append(frame)
                pos = sample_pos  # advance past this frame

            except (ValueError, IndexError):
                pos += 1  # skip one sample and retry

        # Keep only the tail at most 2 frame-lengths of samples for re-scan
        keep = max(0, len(self._buf) - DIGITAL_BUFFER_SIZE)
        self._buf = self._buf[keep:]


# MessageAssembler mirrors CANapp exactly
class MessageAssembler:
    def __init__(self):
        self.buffers = {}

    def reset(self):
        self.buffers.clear()

    def process(self, msg: dict) -> dict | None:
        data = [int(h, 16) for h in msg["hex"]]
        if len(data) < 2:
            return None
        seq  = data[0]
        flag = data[1]
        payload = bytes(data[2:])
        can_id  = msg["id"]
        if flag != 0x00:
            return None
        if seq == 0x01:
            self.buffers[can_id] = {"seq": 1, "data": bytearray(payload), "ts": msg["ts"], "id": can_id}
            return None
        if seq > 0x01:
            buf = self.buffers.get(can_id)
            if not buf:
                return None
            if seq != buf["seq"] + 1:
                del self.buffers[can_id]
                return None
            buf["data"].extend(payload)
            buf["seq"] = seq
            raw = bytes(buf["data"]).rstrip(b"\x00")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
            if text.count("{") > 0 and text.count("{") == text.count("}"):
                try:
                    parsed = json.loads(text)
                    pretty = json.dumps(parsed, indent=2)
                    result = {"ts": buf["ts"], "id": buf["id"], "frames": seq,
                              "raw": text, "pretty": pretty, "is_json": True}
                except json.JSONDecodeError:
                    result = {"ts": buf["ts"], "id": buf["id"], "frames": seq,
                              "raw": text, "pretty": text, "is_json": False}
                del self.buffers[can_id]
                return result
        return None


_assembler = MessageAssembler()


# ── Digital capture worker ─────────────────────────────────────────────────────
def _digital_worker(dev_index: int, dio_channel: int, invert: bool = False):
    """Continuously capture digital samples from AD3 DIO pin and decode CAN."""
    global _last_status
    if not _DWF_AVAILABLE:
        return

    decoder = CANDecoder()
    local_assembler = MessageAssembler()

    def emit_status(msg, running=True, error=False):
        global _last_status
        payload = {"msg": msg, "running": running, "error": error}
        _last_status = payload
        socketio.emit("status", payload)

    try:
        with _dwf.deviceControl.open(dev_index) as device:
            dig = device.digitalIn

            # Configure digital input
            dig.reset()
            dig.acquisitionModeSet(DwfAcquisitionMode.ScanShift)
            dig.clockSourceSet(DwfDigitalInClockSource.Internal)
            # Query actual internal clock and compute divider
            system_freq = dig.internalClockInfo()   # typically 100 MHz on AD3
            divider = max(1, int(system_freq) // DIGITAL_SAMPLE_RATE)
            dig.dividerSet(divider)
            actual_rate = system_freq / divider
            dig.sampleFormatSet(8)          # 8-bit samples (DIO 0-7)
            n_samples = DIGITAL_BUFFER_SIZE
            dig.bufferSizeSet(n_samples)

            emit_status(
                f"Digital capture active — DIO{dio_channel} @ "
                f"{actual_rate/1e6:.2f} Msps ({CAN_BITRATE//1000} kbps CAN)"
            )
            _log_write(
                f"[{datetime.now().strftime('%H:%M:%S')}] DIGITAL START | "
                f"DIO{dio_channel} | {actual_rate/1e6:.2f} Msps"
            )

            dig.configure(False, True)   # reconfigure=False, start=True

            while not _digital_stop.is_set():
                # Poll for available samples
                status = dig.status(True)
                avail = dig.statusSamplesValid()

                if avail >= MIN_DECODE_SAMPLES:
                    raw = dig.statusData(avail)
                    # Extract the target channel bit
                    mask = 1 << dio_channel
                    samples = [(int(b) >> dio_channel) & 1 for b in raw]
                    if invert:
                        samples = [1 - s for s in samples]
                    decoder.feed(samples)

                    frames = decoder.pop_frames()
                    for f in frames:
                        with _replay_lock:
                            _frame_buf.append(f)
                        socketio.emit("frame", f)

                        hex_str = " ".join(f["hex"])
                        _log_write(
                            f"[{f['ts']}] FRAME    | ID: {f['id']:<12} | "
                            f"DLC: {f['dlc']} | Hex: {hex_str:<27} | ASCII: {f['text']}"
                        )

                        assembled = local_assembler.process(f)
                        if assembled:
                            socketio.emit("message", assembled)
                            tag = "JSON" if assembled["is_json"] else "TEXT"
                            _log_write(
                                f"[{assembled['ts']}] MESSAGE  | ID: {assembled['id']:<12} | "
                                f"Frames: {assembled['frames']} | Type: {tag} | Raw: {assembled['raw']}"
                            )
                else:
                    time.sleep(0.005)

    except Exception as e:
        emit_status(f"Digital error: {e}", running=False, error=True)
        _log_write(f"[{datetime.now().strftime('%H:%M:%S')}] ERROR | {e}")
    finally:
        emit_status("Digital capture stopped", running=False, error=False)
        with _replay_lock:
            _frame_buf.clear()


# ── Analog snapshot ────────────────────────────────────────────────────────────
def _do_analog_snapshot(dev_index: int, ch1_en: bool, ch2_en: bool,
                         v_range: float, v_offset: float) -> dict:
    """
    Capture a single oscilloscope snapshot.
    Returns {"ok": True, "ch1": [...], "ch2": [...], "time_us": [...], "sample_rate": ...}
    or      {"ok": False, "error": "..."}
    """
    if not _DWF_AVAILABLE:
        return {"ok": False, "error": "libdwf not available"}
    try:
        with _dwf.deviceControl.open(dev_index) as device:
            scope = device.analogIn
            scope.reset()

            record_duration = ANALOG_BUFFER_SIZE / ANALOG_SAMPLE_RATE
            scope.acquisitionModeSet(DwfAcquisitionMode.Record)
            scope.frequencySet(ANALOG_SAMPLE_RATE)
            scope.recordLengthSet(record_duration)

            scope.channelEnableSet(0, ch1_en)
            scope.channelRangeSet(0, v_range)
            scope.channelOffsetSet(0, v_offset)
            scope.channelFilterSet(0, DwfAnalogInFilter.Average)

            scope.channelEnableSet(1, ch2_en)
            scope.channelRangeSet(1, v_range)
            scope.channelOffsetSet(1, v_offset)
            scope.channelFilterSet(1, DwfAnalogInFilter.Average)

            scope.triggerSourceSet(DwfTriggerSource.None_)
            scope.configure(True, True)

            import numpy as np
            ch1_buf = np.empty(ANALOG_BUFFER_SIZE, dtype=np.float64)
            ch2_buf = np.empty(ANALOG_BUFFER_SIZE, dtype=np.float64)
            total = 0
            deadline = time.time() + 5.0

            while total < ANALOG_BUFFER_SIZE and time.time() < deadline:
                state = scope.status(True)
                avail, lost, corrupt = scope.statusRecord()
                if avail > 0:
                    chunk = min(avail, ANALOG_BUFFER_SIZE - total)
                    if ch1_en:
                        ch1_buf[total:total+chunk] = scope.statusData(0, chunk)[:chunk]
                    if ch2_en:
                        ch2_buf[total:total+chunk] = scope.statusData(1, chunk)[:chunk]
                    total += chunk
                time.sleep(0.005)

            if total < ANALOG_BUFFER_SIZE:
                return {"ok": False, "error": f"Snapshot incomplete: {total}/{ANALOG_BUFFER_SIZE}"}

            step = max(1, ANALOG_BUFFER_SIZE // 2000)
            result: dict = {
                "ok": True,
                "sample_rate": ANALOG_SAMPLE_RATE,
                "capture_sec": ANALOG_CAPTURE_SEC,
                "ch1": [round(float(v), 4) for v in ch1_buf[::step]] if ch1_en else [],
                "ch2": [round(float(v), 4) for v in ch2_buf[::step]] if ch2_en else [],
            }
            n = max(len(result["ch1"]), len(result["ch2"]))
            step_us = (ANALOG_CAPTURE_SEC / n) * 1e6 if n else 1
            result["time_us"] = [round(i * step_us, 3) for i in range(n)]
            return result

    except Exception as e:
        return {"ok": False, "error": str(e)}



# ── Graceful shutdown ──────────────────────────────────────────────────────────
def _shutdown_all(signum=None, frame=None):
    _digital_stop.set()
    _log_close()
    os._exit(0)

signal.signal(signal.SIGTERM, _shutdown_all)
signal.signal(signal.SIGINT,  _shutdown_all)


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ── SocketIO events ────────────────────────────────────────────────────────────
@socketio.on("connect")
def handle_connect():
    emit("status", _last_status)
    emit("dwf_status", {
        "available":  _DWF_AVAILABLE,
        "error":      _DWF_ERROR,
    })
    with _replay_lock:
        for f in _frame_buf:
            emit("frame", f)


@socketio.on("scan_devices")
def handle_scan_devices():
    devices = scan_ad3_devices()
    emit("device_list", {"devices": devices, "dwf_available": _DWF_AVAILABLE,
                          "dwf_error": _DWF_ERROR})


@socketio.on("start_digital")
def handle_start_digital(data):
    global _digital_thread
    with _digital_lock:
        if _digital_thread and _digital_thread.is_alive():
            emit("status", {"msg": "Already running", "running": True, "error": False})
            return

        dev_index   = int(data.get("device_index", 0))
        dio_channel = int(data.get("dio_channel", 0))
        invert      = bool(data.get("invert", False))

        _digital_stop.clear()
        _assembler.reset()
        with _replay_lock:
            _frame_buf.clear()

        _digital_thread = threading.Thread(
            target=_digital_worker,
            args=(dev_index, dio_channel, invert),
            daemon=True,
        )
        _digital_thread.start()

    _log_open()
    emit("status", {"msg": f"Starting digital capture on DIO{dio_channel}…",
                     "running": True, "error": False})


@socketio.on("stop_digital")
def handle_stop_digital():
    _digital_stop.set()
    _log_close()
    emit("status", {"msg": "Stopping…", "running": False, "error": False})


def _stop_digital_and_wait(timeout: float = 3.0):
    """Signal the digital worker to stop and block until its thread exits."""
    global _digital_thread
    _digital_stop.set()
    with _digital_lock:
        t = _digital_thread
    if t and t.is_alive():
        t.join(timeout=timeout)
    _log_close()


@socketio.on("analog_snapshot")
def handle_analog_snapshot(data):
    dev_index  = int(data.get("device_index", 0))
    ch1_en     = bool(data.get("ch1", True))
    ch2_en     = bool(data.get("ch2", True))
    v_range    = float(data.get("v_range", 5.0))
    v_offset   = float(data.get("v_offset", 0.0))

    emit("snapshot_status", {"msg": "Stopping digital capture…", "busy": True})
    _stop_digital_and_wait()
    emit("snapshot_status", {"msg": "Capturing…", "busy": True})

    with _analog_lock:
        result = _do_analog_snapshot(dev_index, ch1_en, ch2_en, v_range, v_offset)

    if result["ok"]:
        emit("snapshot_data", result)
        emit("snapshot_status", {"msg": "Snapshot complete", "busy": False})
    else:
        emit("snapshot_status", {"msg": f"Error: {result['error']}", "busy": False})


@socketio.on("clear_frames")
def handle_clear_frames():
    with _replay_lock:
        _frame_buf.clear()


@socketio.on("run_health_check")
def handle_health_check(data):
    dev_index = int(data.get("device_index", 0))
    emit("health_check_status", {"msg": "Stopping digital capture…", "busy": True})
    _stop_digital_and_wait()
    emit("health_check_status", {"msg": "Capturing at 4 Msps — waiting for trigger…", "busy": True})
    with _analog_lock:
        result = _do_analog_health_check(dev_index)
    if result["ok"]:
        emit("health_check_result", result)
        n_issues = len(result.get("metrics", {}).get("issues", []))
        verdict  = result.get("metrics", {}).get("overall", "?")
        emit("health_check_status", {
            "msg":  f"Health check complete — {verdict}  ({n_issues} issue(s))",
            "busy": False,
            "ok":   verdict == "PASS",
        })
    else:
        emit("health_check_status", {
            "msg":  f"Error: {result.get('error', 'unknown')}",
            "busy": False,
            "ok":   False,
        })


if __name__ == "__main__":
    print(f"CANdiscovery running at http://0.0.0.0:5001")
    print(f"Logs: {LOG_DIR}")
    if not _DWF_AVAILABLE:
        print(f"[WARN] libdwf not available: {_DWF_ERROR}")
        print("       Install WaveForms runtime from https://digilent.com/reference/software/waveforms/waveforms-3/start")
    socketio.run(app, host="0.0.0.0", port=5001, debug=False, allow_unsafe_werkzeug=True)
