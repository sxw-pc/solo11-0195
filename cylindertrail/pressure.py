"""Joint pressure/temperature feasible-state engine for returned cylinders.

The fixed :func:`drop` is kept for the seeded regression: the raw pressure
bytes alone say only that 100 became 90.  Physics needs the paired Kelvin
temperatures (same bottle, same closed chamber, same clock anchor) before
cooling and leakage can be separated, and even then *all three feasible
worlds* -- cooling, leakage, gauge drift -- must be listed inside the
measurement error bounds instead of collapsing to a threshold.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .pressure_capture import PairedSample, intervals_overlap, apply_uncertainty


def drop(readings):
    """Raw apparent pressure loss of the seeded fixture (fixed behaviour)."""
    first, last = readings[0], readings[-1]
    return first['pressure'] - last['pressure']


def cooling_expected_pressure(p1: float, t1_k: float, t2_k: float) -> float:
    """Constant-mass, constant-volume ideal-gas pressure after cooling."""
    if t1_k <= 0 or t2_k <= 0:
        raise ValueError("thermodynamic temperature must be positive Kelvin")
    return p1 * t2_k / t1_k


def mass_ratio(p1: float, t1_k: float, p2: float, t2_k: float) -> float:
    """n2/n1 under ideal gas at fixed volume; 1.0 means the gas amount held."""
    return (p2 * t1_k) / (p1 * t2_k)


def _pressure_interval(sample: PairedSample) -> tuple[float, float]:
    return apply_uncertainty(sample.pressure, sample.pressure_u)


def _temperature_interval(sample: PairedSample) -> tuple[float, float]:
    return apply_uncertainty(sample.temperature_k, sample.temperature_u)


def _mass_balance(mass_readings: Sequence[dict] | None) -> dict[str, Any]:
    if not mass_readings:
        return {"available": False, "mass_constant": None, "mass_lost": None,
                "interval_1": None, "interval_2": None}
    ordered = sorted(mass_readings, key=lambda r: r["at"])
    first, last = ordered[0], ordered[-1]
    i1 = apply_uncertainty(float(first["value"]), float(first.get("uncertainty", 0.0)))
    i2 = apply_uncertainty(float(last["value"]), float(last.get("uncertainty", 0.0)))
    # Mass lost only when the later interval sits strictly below the earlier one.
    mass_lost = i2[1] < i1[0]
    mass_constant = intervals_overlap(i1, i2)
    return {"available": True, "mass_constant": mass_constant, "mass_lost": mass_lost,
            "interval_1": i1, "interval_2": i2,
            "evidence": [r.get("id") for r in ordered]}


def _zone_detector(detector_readings: Sequence[dict] | None) -> dict[str, Any]:
    if not detector_readings:
        return {"available": False, "positive": None, "readings": []}
    parsed = [{"id": r.get("id"), "at": r["at"], "zone": r.get("zone"),
               "concentration": r.get("concentration"),
               "positive": bool(r.get("positive"))} for r in detector_readings]
    positive = any(item["positive"] for item in parsed)
    return {"available": True, "positive": positive, "readings": parsed}


@dataclass(frozen=True)
class ConflictWitness:
    """Smallest concrete numeric contradiction that rules out a feasible world."""
    world: str
    constraint: str
    expected_interval: tuple[float, float]
    observed_interval: tuple[float, float]
    explanation: str

    def as_dict(self) -> dict[str, Any]:
        return {"world": self.world, "constraint": self.constraint,
                "expected_interval": self.expected_interval,
                "observed_interval": self.observed_interval,
                "explanation": self.explanation}


def joint_feasible_states(samples: Sequence[PairedSample],
                          mass_readings: Sequence[dict] | None = None,
                          detector_readings: Sequence[dict] | None = None) -> dict[str, Any]:
    """List cooling / leak / gauge-drift worlds inside the measurement bounds.

    ``samples`` are same-bottle, same-chamber, co-anchored pairs produced by
    :func:`cylindertrail.pressure_capture.pair_samples`.  With no trustworthy
    temperature (empty list) the pressure bytes stay compatible with both
    cooling and leakage, so both worlds remain feasible and no release can be
    derived from a single temperature average.
    """
    if not samples:
        return {
            "bottle_states": [],
            "worlds": {"cooling": {"feasible": True, "witness": None},
                       "leak": {"feasible": True, "witness": None},
                       "gauge_drift": {"feasible": True, "witness": None}},
            "mass_balance": _mass_balance(mass_readings),
            "zone_detector": _zone_detector(detector_readings),
            "raw_pressure_drop": None,
            "note": "no co-anchored temperature: pressure alone cannot separate worlds",
        }

    first, last = samples[0], samples[-1]
    p1_i, p2_i = _pressure_interval(first), _pressure_interval(last)
    t1_i, t2_i = _temperature_interval(first), _temperature_interval(last)
    raw_drop = round(first.pressure - last.pressure, 6)

    # Cooling world: is some (t1, t2) inside the temperature error bounds able
    # to reproduce the observed p2 with constant mass?  Bounding extrema of
    # p1*t2/t1 occur at (max p1, max t2, min t1) and the opposite corner.
    cool_hi = cooling_expected_pressure(p1_i[1], t1_i[0], t2_i[1])
    cool_lo = cooling_expected_pressure(p1_i[0], t1_i[1], t2_i[0])
    cooling_interval = (round(cool_lo, 6), round(cool_hi, 6))
    cooling_feasible = intervals_overlap(cooling_interval, p2_i)
    cooling_witness = None if cooling_feasible else ConflictWitness(
        "cooling", "p2 == p1 * t2 / t1 (constant mass)", cooling_interval, p2_i,
        "no temperature inside the calibrated error bounds reproduces p2").as_dict()

    # Leak world: excluded only when the whole observed pressure band is
    # explainable by constant-mass cooling (the observed band sits inside the
    # cooling prediction band).  Any observed value below the cooling floor
    # means some gas must have escaped.
    leak_feasible = not (p2_i[0] >= cool_lo and p2_i[1] <= cool_hi)
    leak_witness = None if leak_feasible else ConflictWitness(
        "leak", "cooling band covers the whole observed p2 band",
        cooling_interval, p2_i,
        "constant-mass cooling explains every observed pressure; no escape needed").as_dict()

    # Gauge-drift world: could one physical pressure be reported as both
    # readings after independent calibration?  Excluded when corrected
    # intervals are disjoint, i.e. the calibrated gauge cannot have drifted
    # enough to manufacture the drop.
    drift_feasible = intervals_overlap(p1_i, p2_i)
    drift_witness = None if drift_feasible else ConflictWitness(
        "gauge_drift", "corrected pressure intervals overlap", p1_i, p2_i,
        "calibrated pressure intervals are disjoint; the gauge cannot explain the drop").as_dict()

    bottle_states = [{
        "seq": s.seq, "at": s.at,
        "pressure": s.pressure, "pressure_interval": _pressure_interval(s),
        "temperature_k": s.temperature_k,
        "temperature_interval": _temperature_interval(s),
        "gauge_id": s.gauge_id, "thermometer_id": s.thermometer_id,
        "pressure_hash": s.pressure_hash, "temperature_hash": s.temperature_hash,
    } for s in samples]
    # Pointwise mass ratio: a direct per-instant witness of gas conservation.
    ratio = mass_ratio(first.pressure, first.temperature_k,
                       last.pressure, last.temperature_k)
    ratio_lo = mass_ratio(p1_i[1], t1_i[0], p2_i[0], t2_i[1])
    ratio_hi = mass_ratio(p1_i[0], t1_i[1], p2_i[1], t2_i[0])

    return {
        "bottle_id": getattr(first, "bottle_id", None),
        "bottle_states": bottle_states,
        "worlds": {"cooling": {"feasible": cooling_feasible, "witness": cooling_witness},
                   "leak": {"feasible": leak_feasible, "witness": leak_witness},
                   "gauge_drift": {"feasible": drift_feasible, "witness": drift_witness}},
        "mass_ratio_point": round(ratio, 6),
        "mass_ratio_interval": (round(ratio_lo, 6), round(ratio_hi, 6)),
        "cooling_prediction_interval": cooling_interval,
        "mass_balance": _mass_balance(mass_readings),
        "zone_detector": _zone_detector(detector_readings),
        "raw_pressure_drop": raw_drop,
    }
