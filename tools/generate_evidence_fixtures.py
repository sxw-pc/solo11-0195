"""Reproducible generator for the signed offline evidence fixtures.

Run once (committed output is checked in); the HMAC keys here are fixed test
keys, never production secrets.  Each bundle is a set of signed originals from
independent instruments for one physical bottle and closed gas chamber.
"""
import json
from pathlib import Path

from cylindertrail.pressure_capture import make_envelope, canonical, content_hash

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "evidence"
KEYS = {
    "key-gauge-g1": b"test-key-gauge-g1-0001",
    "key-thermo-t1": b"test-key-thermo-t1-0001",
    "key-scale-s1": b"test-key-scale-s1-0001",
    "key-detector-d1": b"test-key-detector-d1-0001",
    "key-clock-c1": b"test-key-clock-c1-0001",
    "key-calib-q1": b"test-key-calib-q1-0001",
}


def envelope(rec, key_id):
    return make_envelope(rec, key_id, KEYS[key_id])


def clock_anchor():
    return envelope({
        "id": "CLK-1", "type": "clock_anchor", "bottle_id": "CYL-1",
        "chamber_id": "CH-1", "device_id": "CLOCK-1", "at": "2026-11-24T08:55",
        "epoch": "EPOCH-2026-11A", "max_offset_seconds": 60,
        "devices": ["GAUGE-G1", "THERMO-T1", "SCALE-S1", "DETECTOR-D1"],
        "anchored_at": "2026-11-24T08:55",
    }, "key-clock-c1")


def calibration(points, gauge="GAUGE-G1", valid_from="2026-11-24T00:00",
                valid_until="2026-11-30T00:00", epoch="EPOCH-2026-11A",
                bottle="CYL-1", chamber="CH-1"):
    return envelope({
        "id": f"CAL-{gauge}", "type": "calibration", "bottle_id": bottle,
        "chamber_id": chamber, "device_id": "LAB-Q1", "at": valid_from,
        "gauge_id": gauge, "valid_from": valid_from,
        "valid_until": valid_until, "epoch": epoch, "points": points,
    }, "key-calib-q1")


def pressure(pid, at, value, uncertainty=0.3, gauge="GAUGE-G1",
             bottle="CYL-1", chamber="CH-1", drift=0.0):
    return {"id": pid, "type": "pressure", "bottle_id": bottle,
            "chamber_id": chamber, "device_id": gauge, "at": at,
            "value": value, "unit": "bar", "uncertainty": uncertainty,
            "drift": drift}


def temperature(tid, at, value, unit="K", uncertainty=1.0,
                thermo="THERMO-T1", bottle="CYL-1", chamber="CH-1",
                epoch="EPOCH-2026-11A"):
    return {"id": tid, "type": "temperature", "bottle_id": bottle,
            "chamber_id": chamber, "device_id": thermo, "at": at,
            "value": value, "unit": unit, "uncertainty": uncertainty,
            "uncertainty_k": uncertainty, "epoch": epoch}


def mass(mid, at, value, uncertainty=0.05):
    return {"id": mid, "type": "mass", "bottle_id": "CYL-1",
            "chamber_id": "CH-1", "device_id": "SCALE-S1", "at": at,
            "value": value, "unit": "kg", "uncertainty": uncertainty}


def detector(did, at, concentration, positive, zone="Z-A"):
    return {"id": did, "type": "gas_detector", "bottle_id": "CYL-1",
            "chamber_id": "CH-1", "device_id": "DETECTOR-D1", "at": at,
            "zone": zone, "concentration": concentration,
            "unit": "ppm", "positive": positive}


def bundle(name, records, key_map):
    envelopes = [envelope(r, key_map[r["device_id"] if r["type"] != "calibration"
                          else "LAB-Q1"]) if isinstance(r, dict) and "type" in r
                 else r for r in records]
    payload = {"bundle_id": name, "bottle_id": "CYL-1", "envelopes": envelopes}
    (OUT / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "keys.json").write_text(
        json.dumps({k: v.decode("utf-8") for k, v in KEYS.items()}, indent=2),
        encoding="utf-8")
    identity_curve = [[90.0, 90.0, 0.2], [100.0, 100.0, 0.2]]
    key_map = {"GAUGE-G1": "key-gauge-g1", "THERMO-T1": "key-thermo-t1",
               "SCALE-S1": "key-scale-s1", "DETECTOR-D1": "key-detector-d1",
               "CLOCK-1": "key-clock-c1", "LAB-Q1": "key-calib-q1"}

    # Route A: sealed valve, independent calibrated 300 -> 270 K, mass holds,
    # zone detector negative. Cooling fully explains 100 -> 90.
    bundle("route-a-cooling", [
        clock_anchor(), calibration(identity_curve),
        envelope(pressure("P1", "2026-11-24T09:00", 100.0), "key-gauge-g1"),
        envelope(pressure("P2", "2026-11-24T10:00", 90.0), "key-gauge-g1"),
        envelope(temperature("T1", "2026-11-24T09:00", 300.0), "key-thermo-t1"),
        envelope(temperature("T2", "2026-11-24T10:00", 270.0), "key-thermo-t1"),
        envelope(mass("M1", "2026-11-24T09:00", 42.00), "key-scale-s1"),
        envelope(mass("M2", "2026-11-24T10:00", 42.00), "key-scale-s1"),
        envelope(detector("D1", "2026-11-24T09:30", 0.4, False), "key-detector-d1"),
        envelope(detector("D2", "2026-11-24T10:00", 0.3, False), "key-detector-d1"),
    ], key_map)

    # Route B: trustworthy constant 300 K, weight drops, zone detection positive.
    bundle("route-b-leak", [
        clock_anchor(), calibration(identity_curve),
        envelope(pressure("P1", "2026-11-24T09:00", 100.0), "key-gauge-g1"),
        envelope(pressure("P2", "2026-11-24T10:00", 90.0), "key-gauge-g1"),
        envelope(temperature("T1", "2026-11-24T09:00", 300.0), "key-thermo-t1"),
        envelope(temperature("T2", "2026-11-24T10:00", 300.0), "key-thermo-t1"),
        envelope(mass("M1", "2026-11-24T09:00", 42.00), "key-scale-s1"),
        envelope(mass("M2", "2026-11-24T10:00", 40.20), "key-scale-s1"),
        envelope(detector("D1", "2026-11-24T09:30", 180.0, True), "key-detector-d1"),
        envelope(detector("D2", "2026-11-24T10:00", 210.0, True), "key-detector-d1"),
    ], key_map)

    # Temperature mismatch: the 270 K original belongs to another bottle/chamber.
    mismatch_temp = dict(temperature("T2X", "2026-11-24T10:00", 270.0,
                                     thermo="THERMO-T9", bottle="CYL-9",
                                     chamber="CH-9"))
    bundle("route-mismatch", [
        clock_anchor(), calibration(identity_curve),
        envelope(pressure("P1", "2026-11-24T09:00", 100.0), "key-gauge-g1"),
        envelope(pressure("P2", "2026-11-24T10:00", 90.0), "key-gauge-g1"),
        envelope(temperature("T1", "2026-11-24T09:00", 300.0), "key-thermo-t1"),
        envelope(mismatch_temp, "key-thermo-t1"),
        envelope(mass("M1", "2026-11-24T09:00", 42.00), "key-scale-s1"),
        envelope(mass("M2", "2026-11-24T10:00", 42.00), "key-scale-s1"),
        envelope(detector("D1", "2026-11-24T10:00", 0.3, False), "key-detector-d1"),
    ], key_map)

    # Calibration drift: the gauge's second-period curve over-reads by ~10 bar;
    # the corrected intervals are disjoint, excluding the drift explanation.
    drift_curve = [[90.0, 79.5, 0.4], [100.0, 89.5, 0.4]]
    bundle("route-gauge-drift", [
        clock_anchor(),
        calibration(drift_curve, valid_from="2026-11-24T00:00"),
        envelope(pressure("P1", "2026-11-24T09:00", 100.0, uncertainty=0.4),
                 "key-gauge-g1"),
        envelope(pressure("P2", "2026-11-24T10:00", 90.0, uncertainty=0.4),
                 "key-gauge-g1"),
        envelope(temperature("T1", "2026-11-24T09:00", 300.0), "key-thermo-t1"),
        envelope(temperature("T2", "2026-11-24T10:00", 300.0), "key-thermo-t1"),
        envelope(mass("M1", "2026-11-24T09:00", 42.00), "key-scale-s1"),
        envelope(mass("M2", "2026-11-24T10:00", 40.20), "key-scale-s1"),
        envelope(detector("D1", "2026-11-24T10:00", 195.0, True), "key-detector-d1"),
    ], key_map)

    print("wrote", sorted(p.name for p in OUT.iterdir()))


if __name__ == "__main__":
    main()
