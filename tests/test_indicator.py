import time

import pytest

from weighbridge.indicator import CaptureError, FrameParser, WeightMonitor

from .conftest import hold

DEFAULT = r"(?P<sign>[-+])?\s*(?P<weight>\d+(?:\.\d+)?)"


@pytest.mark.parametrize("data, expected", [
    (b"\x02+0012340\x03", 12340),
    (b"ST,GS,+0012340kg\r\n", 12340),
    (b"   45670\r", 45670),
    (b"W1  38220 kg\r\n", 38220),
    (b"-00000020\r\n", -20),
])
def test_parser_common_formats(data, expected):
    out = FrameParser(DEFAULT).feed(data)
    assert out and out[0][0] == expected


def test_parser_handles_split_frames_and_tonnes():
    p = FrameParser(DEFAULT, multiplier=1000)
    assert p.feed(b"12.3") == []
    assert p.feed(b"45\r\n")[0][0] == 12345.0


def test_parser_status_token():
    p = FrameParser(r"(?P<status>ST|US),GS,(?P<sign>[-+])(?P<weight>\d+)", status_stable="ST")
    stable, moving = p.feed(b"ST,GS,+0012340\r\nUS,GS,+0012380\r\n")
    assert stable[1] is True and moving[1] is False


def test_parser_rejects_pattern_without_weight_group():
    with pytest.raises(ValueError):
        FrameParser(r"\d+")


def test_monitor_stability(cfg):
    m = WeightMonitor(cfg)
    assert m.snapshot()["ok"] is False  # nothing received yet
    now = time.monotonic()
    for i in range(20):  # truck settling: swings of 200 kg
        m.add(40000 + (200 if i % 2 else 0), at=now - 4 + i * 0.2)
    assert m.snapshot()["stable"] is False
    with pytest.raises(CaptureError, match="not stable"):
        m.capture()


def test_monitor_capture_when_stable(cfg):
    m = WeightMonitor(cfg)
    hold(m, 41230)
    snap = m.snapshot()
    assert snap["stable"] and snap["message"] == "Stable"
    assert m.capture() == 41230


def test_monitor_rejects_empty_over_capacity_and_stale(cfg):
    m = WeightMonitor(cfg)
    hold(m, 100)
    with pytest.raises(CaptureError, match="empty"):
        m.capture()
    m2 = WeightMonitor(cfg)
    hold(m2, 150000)
    with pytest.raises(CaptureError, match="capacity"):
        m2.capture()
    m3 = WeightMonitor(cfg)
    m3.add(40000, at=time.monotonic() - 10)
    with pytest.raises(CaptureError, match="No signal"):
        m3.capture()


def test_monitor_needs_full_window(cfg):
    m = WeightMonitor(cfg)
    hold(m, 40000, seconds=1.0)  # only 1 s of readings; config asks for 3 s
    assert m.snapshot()["stable"] is False
