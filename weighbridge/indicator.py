"""Reads the weight indicator and decides when a weight is stable.

The indicator is the only source of weight. The browser never sends a weight
number; captures read WeightMonitor.capture() on the server.
"""
from __future__ import annotations

import logging
import random
import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass

log = logging.getLogger(__name__)

FRAME_ENDS = (b"\r", b"\n", b"\x03")


@dataclass
class Reading:
    at: float
    kg: float
    status_ok: bool
    raw: str


class FrameParser:
    """Turns raw bytes into weights using the regex from config."""

    def __init__(self, pattern: str, multiplier: float = 1.0, status_stable: str = ""):
        self.regex = re.compile(pattern)
        if "weight" not in self.regex.groupindex:
            raise ValueError("indicator.pattern must contain a (?P<weight>...) group")
        self.multiplier = multiplier
        self.status_stable = status_stable
        self._buf = b""

    def feed(self, data: bytes) -> list[tuple[float, bool, str]]:
        self._buf += data
        frames: list[bytes] = []
        while True:
            cut = min((i for i in (self._buf.find(e) for e in FRAME_ENDS) if i >= 0), default=-1)
            if cut < 0:
                break
            frames.append(self._buf[:cut])
            self._buf = self._buf[cut + 1:]
        if len(self._buf) > 256:  # indicator without terminators: parse what we have
            frames.append(self._buf)
            self._buf = b""
        out = []
        for frame in frames:
            parsed = self.parse_frame(frame)
            if parsed is not None:
                out.append(parsed)
        return out

    def parse_frame(self, frame: bytes) -> tuple[float, bool, str] | None:
        text = frame.replace(b"\x02", b"").decode("latin-1")
        text = "".join(ch for ch in text if ch.isprintable())
        if not text.strip():
            return None
        matches = list(self.regex.finditer(text))
        if not matches:
            return None
        # Prefer the match with the most digits (the weight, not a channel or unit number).
        best = max(matches, key=lambda m: (len(re.sub(r"\D", "", m.group("weight"))), m.start()))
        try:
            value = float(best.group("weight"))
        except ValueError:
            return None
        sign = best.groupdict().get("sign")
        if sign == "-":
            value = -value
        status_ok = True
        if self.status_stable:
            status = best.groupdict().get("status")
            status_ok = self.status_stable in (status if status is not None else text)
        return round(value * self.multiplier, 3), status_ok, text.strip()


class WeightMonitor:
    """Keeps recent readings and answers: what is on the platform, and is it stable?"""

    def __init__(self, cfg: dict):
        ind = cfg["indicator"]
        self.stable_seconds = float(ind["stable_seconds"])
        self.tolerance = float(ind["stable_tolerance_kg"])
        self.stale_after = float(ind["stale_after_seconds"])
        self.min_weight = float(ind["min_weight_kg"])
        self.capacity = float(ind["capacity_kg"])
        self.source = ind["source"]
        self._lock = threading.Lock()
        self._readings: deque[Reading] = deque()
        self.connected = False
        self.error = ""

    def add(self, kg: float, status_ok: bool = True, raw: str = "", at: float | None = None) -> None:
        at = time.monotonic() if at is None else at
        with self._lock:
            self._readings.append(Reading(at, kg, status_ok, raw))
            horizon = at - max(10.0, self.stable_seconds * 3)
            while self._readings and self._readings[0].at < horizon:
                self._readings.popleft()

    def snapshot(self, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        with self._lock:
            readings = list(self._readings)
        if not readings or now - readings[-1].at > self.stale_after:
            return {"ok": False, "kg": None, "stable": False, "raw": "",
                    "message": self.error or "No signal from indicator", "source": self.source}
        latest = readings[-1]
        window = [r for r in readings if r.at >= now - self.stable_seconds]
        covered = readings[0].at <= now - self.stable_seconds + 0.25
        stable = (
            covered and len(window) >= 2
            and max(r.kg for r in window) - min(r.kg for r in window) <= self.tolerance
            and all(r.status_ok for r in window)
        )
        if latest.kg > self.capacity:
            message = "Over capacity"
        elif latest.kg < self.min_weight:
            message = "Platform empty"
        elif stable:
            message = "Stable"
        else:
            message = "Settling…"
        return {"ok": True, "kg": latest.kg, "stable": stable, "raw": latest.raw,
                "message": message, "source": self.source}

    def capture(self) -> float:
        """Return the stable weight or raise CaptureError explaining why not."""
        snap = self.snapshot()
        if not snap["ok"]:
            raise CaptureError(snap["message"])
        if snap["kg"] > self.capacity:
            raise CaptureError(f"Weight {snap['kg']:.0f} kg is above the platform capacity.")
        if snap["kg"] < self.min_weight:
            raise CaptureError("The platform is empty. Drive the vehicle fully onto the platform.")
        if not snap["stable"]:
            raise CaptureError("Weight is not stable yet. Wait for the vehicle to settle.")
        return snap["kg"]


class CaptureError(Exception):
    pass


# --- sources -----------------------------------------------------------------

class _ReaderThread(threading.Thread):
    def __init__(self, monitor: WeightMonitor, parser: FrameParser | None):
        super().__init__(daemon=True, name=self.__class__.__name__)
        self.monitor = monitor
        self.parser = parser
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _deliver(self, data: bytes) -> None:
        for kg, status_ok, raw in self.parser.feed(data):
            self.monitor.add(kg, status_ok, raw)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.loop()
            except Exception as exc:  # keep reading after cable pulls, port errors, etc.
                self.monitor.connected = False
                self.monitor.error = f"Indicator error: {exc}"
                log.warning("indicator: %s", exc)
                self._stop.wait(2.0)

    def loop(self) -> None:
        raise NotImplementedError


class SerialReader(_ReaderThread):
    def __init__(self, monitor, parser, ind: dict):
        super().__init__(monitor, parser)
        self.ind = ind

    def loop(self) -> None:
        import serial  # pyserial

        with serial.Serial(
            port=self.ind["port"], baudrate=int(self.ind["baudrate"]),
            bytesize=int(self.ind["bytesize"]), parity=self.ind["parity"],
            stopbits=self.ind["stopbits"],
            timeout=0.5,
        ) as port:
            self.monitor.connected, self.monitor.error = True, ""
            while not self._stop.is_set():
                data = port.read(256)
                if data:
                    self._deliver(data)


class TcpReader(_ReaderThread):
    def __init__(self, monitor, parser, ind: dict):
        super().__init__(monitor, parser)
        self.ind = ind

    def loop(self) -> None:
        with socket.create_connection((self.ind["host"], int(self.ind["tcp_port"])), timeout=5) as sock:
            sock.settimeout(5)
            self.monitor.connected, self.monitor.error = True, ""
            while not self._stop.is_set():
                data = sock.recv(256)
                if not data:
                    raise ConnectionError("indicator closed the connection")
                self._deliver(data)


class SimulatorReader(_ReaderThread):
    """Pretend trucks for demos and training: empty, drive on, settle, drive off."""

    def __init__(self, monitor):
        super().__init__(monitor, None)

    def loop(self) -> None:
        self.monitor.connected, self.monitor.error = True, ""
        while not self._stop.is_set():
            target = random.choice([random.randint(11000, 16000), random.randint(34000, 48000)])
            target = round(target / 10) * 10
            for kg, seconds in ((0, 8.0), (None, 4.0), (target, 25.0), (0, 2.0)):
                end = time.monotonic() + seconds
                while time.monotonic() < end and not self._stop.is_set():
                    if kg is None:  # driving on: rising, noisy
                        progress = 1 - (end - time.monotonic()) / seconds
                        value = target * progress + random.randint(-400, 400)
                    else:
                        value = kg
                    value = max(0, round(value / 10) * 10)
                    self.monitor.add(value, True, f"SIM {value:.0f} kg")
                    time.sleep(0.2)


def start_reader(cfg: dict, monitor: WeightMonitor) -> _ReaderThread:
    ind = cfg["indicator"]
    source = ind["source"]
    if source == "simulator":
        reader = SimulatorReader(monitor)
    else:
        parser = FrameParser(ind["pattern"], float(ind["multiplier"]), ind.get("status_stable", ""))
        reader = SerialReader(monitor, parser, ind) if source == "serial" else TcpReader(monitor, parser, ind)
    reader.start()
    return reader


def sniff(port: str, bauds: tuple[int, ...] = (9600, 4800, 2400, 19200, 1200), seconds: float = 3.0) -> None:
    """Print what the indicator sends at each common baud rate, to help set pattern/baudrate."""
    import serial

    for baud in bauds:
        print(f"\n=== {port} @ {baud} baud, 8N1 — reading {seconds:.0f} s ===")
        try:
            with serial.Serial(port, baud, timeout=0.3) as ser:
                end, data = time.monotonic() + seconds, b""
                while time.monotonic() < end:
                    data += ser.read(256)
        except Exception as exc:
            print(f"  could not open port: {exc}")
            return
        if not data:
            print("  (nothing received — check cable, COM port number and that the indicator's "
                  "serial output is set to 'continuous')")
            continue
        printable = sum(32 <= b < 127 or b in (2, 3, 10, 13) for b in data) / len(data)
        print(f"  {len(data)} bytes, {printable:.0%} readable")
        print("  raw:", repr(data[:200]))
        if printable > 0.9:
            parser = FrameParser(r"(?P<sign>[-+])?\s*(?P<weight>\d+(?:\.\d+)?)")
            found = parser.feed(data)[:5]
            print("  weights found with the default pattern:", [f[0] for f in found] or "none")
            print(f"  → this baud rate looks right. Put baudrate = {baud} in config.toml")
            return
    print("\nNo readable data at any baud rate. Try parity E or O, or check the indicator manual.")
