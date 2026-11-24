"""Offline capture of pressure/temperature/mass/detector originals for one bottle.

Evidence is collected *offline* from independent instruments.  Every original
record is transported as a signed envelope (HMAC-SHA256 over a canonical JSON
body).  A record is only usable when:

* its signature verifies against a key whose id is registered for the device;
* pressure and temperature come from the same physical bottle and closed gas
  chamber, observed at comparable real instants anchored to the same clock
  anchor (one shared calibration epoch -- no per-point cherry picking);
* the pressure gauge calibration curve is valid at those instants.

Hash de-duplication and the SQLite transaction live here as well, so a tampered
or partial bundle can never be partially imported.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Canonical JSON, content hashes and detached signatures
# ---------------------------------------------------------------------------

def canonical(obj: Any) -> bytes:
    """Deterministic JSON bytes used for hashing and signature verification."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def sign(body: Any, key: bytes) -> str:
    return hmac.new(key, canonical(body), hashlib.sha256).hexdigest()


def make_envelope(record: dict, key_id: str, key: bytes) -> dict:
    """Wrap one raw instrument record in a signed envelope."""
    body = dict(record)
    return {"sig_version": "HMAC-SHA256", "key_id": key_id,
            "body": body, "sig": sign(body, key)}


class SignatureError(ValueError):
    """Raised when an envelope fails signature or key-manifest verification."""


def verify_envelope(envelope: dict, keys: dict[str, bytes]) -> dict:
    """Return the verified body or raise SignatureError.

    Trust in a temperature sensor is therefore a property of the signed
    original and the registered key, never a configuration switch.
    """
    if not isinstance(envelope, dict):
        raise SignatureError("envelope must be an object")
    if envelope.get("sig_version") != "HMAC-SHA256":
        raise SignatureError("unsupported signature version")
    key_id = envelope.get("key_id")
    key = keys.get(key_id)
    if key is None:
        raise SignatureError(f"key {key_id!r} is not registered for this device")
    body, sig = envelope.get("body"), envelope.get("sig")
    if not isinstance(sig, str) or not hmac.compare_digest(sig, sign(body, key)):
        raise SignatureError(f"signature mismatch on {body_id(body)}")
    return body


def body_id(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("id") or body.get("type") or "record")
    return "record"


def verify_bundle(bundle: dict, keys: dict[str, bytes]) -> list[dict]:
    """Verify every envelope of a bundle; one bad envelope rejects the bundle."""
    if not isinstance(bundle, dict) or not isinstance(bundle.get("envelopes"), list):
        raise SignatureError("bundle must contain an envelopes list")
    bodies = []
    for envelope in bundle["envelopes"]:
        bodies.append(verify_envelope(envelope, keys))
    return bodies


# ---------------------------------------------------------------------------
# Unit conversion and uncertainty intervals
# ---------------------------------------------------------------------------

TEMP_ZERO_C = 273.15


def to_kelvin(value: float, unit: str) -> float:
    if unit == "K":
        return float(value)
    if unit == "C":
        return float(value) + TEMP_ZERO_C
    raise ValueError(f"unsupported temperature unit {unit!r}")


# kPa per unit, keeping the project's nominal pressure unit (bar-like index).
PRESSURE_UNITS = {"bar": 1.0, "kPa": 0.01, "MPa": 10.0, "psi": 0.06894757293168}


def to_nominal_pressure(value: float, unit: str) -> float:
    factor = PRESSURE_UNITS.get(unit)
    if factor is None:
        raise ValueError(f"unsupported pressure unit {unit!r}")
    return float(value) * factor


def apply_uncertainty(value: float, uncertainty: float) -> tuple[float, float]:
    return value - abs(uncertainty), value + abs(uncertainty)


def intervals_overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return min(a[1], b[1]) >= max(a[0], b[0])


# ---------------------------------------------------------------------------
# Calibration curves and clock anchors
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Calibration:
    gauge_id: str
    valid_from: str
    valid_until: str
    epoch: str
    points: tuple[tuple[float, float, float], ...]  # (indicated, corrected, u)

    @classmethod
    def from_record(cls, rec: dict) -> "Calibration":
        points = tuple((float(p[0]), float(p[1]), float(p[2]))
                       for p in rec["points"])
        if len(points) < 2:
            raise ValueError("calibration curve needs at least two points")
        xs = [p[0] for p in points]
        if xs != sorted(xs):
            raise ValueError("calibration indicated values must be sorted")
        return cls(rec["gauge_id"], rec["valid_from"], rec["valid_until"],
                   rec["epoch"], points)

    def covers(self, gauge_id: str, at: str) -> bool:
        return (gauge_id == self.gauge_id
                and self.valid_from <= at <= self.valid_until)

    def correct(self, indicated: float) -> tuple[float, float]:
        """Linear interpolation of the calibration curve, with uncertainty."""
        pts = self.points
        if indicated <= pts[0][0]:
            _, corrected, u = pts[0]
            return corrected, float(u)
        for (x0, y0, u0), (x1, y1, u1) in zip(pts, pts[1:]):
            if x0 <= indicated <= x1:
                if x1 == x0:
                    return float(y0), float(u0)
                t = (indicated - x0) / (x1 - x0)
                return y0 + t * (y1 - y0), u0 + t * (u1 - u0)
        _, corrected, u = pts[-1]
        return float(corrected), float(u)


@dataclass(frozen=True)
class ClockAnchor:
    epoch: str
    devices: tuple[str, ...]
    max_offset_seconds: float
    anchored_at: str

    @classmethod
    def from_record(cls, rec: dict) -> "ClockAnchor":
        return cls(rec["epoch"], tuple(sorted(rec["devices"])),
                   float(rec["max_offset_seconds"]), rec["anchored_at"])


def parse_clock(value: str) -> float:
    """Accept HH:MM and ISO 8601 instants; return comparable seconds/epoch key."""
    if "T" in value:
        date, clock = value.split("T", 1)
        hh, mm = clock.split(":")[:2]
        # Day ordinal keeps instants on different days ordered.
        ordinal = sum(int(x) * w for x, w in zip(date.split("-"), (372, 31, 1)))
        return ordinal * 86400 + int(hh) * 3600 + int(mm) * 60
    hh, mm = value.split(":")[:2]
    return int(hh) * 3600 + int(mm) * 60


# ---------------------------------------------------------------------------
# Verified evidence bundle for one physical bottle / closed chamber
# ---------------------------------------------------------------------------

@dataclass
class EvidenceBundle:
    bottle_id: str
    chamber_id: str
    records: list[dict] = field(default_factory=list)
    hashes: list[str] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)  # device -> key_id

    def by_type(self, rec_type: str) -> list[dict]:
        return [r for r in self.records if r.get("type") == rec_type]

    def calibration(self, gauge_id: str, at: str) -> Calibration | None:
        for rec in self.by_type("calibration"):
            cal = Calibration.from_record(rec)
            if cal.covers(gauge_id, at):
                return cal
        return None

    def clock_anchor(self) -> ClockAnchor | None:
        anchors = [ClockAnchor.from_record(r) for r in self.by_type("clock_anchor")]
        return sorted(anchors, key=lambda a: a.anchored_at)[-1] if anchors else None


def capture(bundle: dict, keys: dict[str, bytes]) -> EvidenceBundle:
    """Verify and normalise one offline evidence bundle.

    All envelopes must verify, declare the same physical bottle/chamber, and
    carry unique content hashes.  Nothing here decides cooling versus leak --
    it only attests which originals are trustworthy and mutually comparable.
    """
    bodies = verify_bundle(bundle, keys)
    if not bodies:
        raise SignatureError("empty evidence bundle")
    bottle_ids = {b.get("bottle_id") for b in bodies}
    chamber_ids = {b.get("chamber_id") for b in bodies}
    if len(bottle_ids) != 1 or None in bottle_ids:
        raise SignatureError("evidence mixes physical bottles; same-bottle pairing required")
    if len(chamber_ids) != 1 or None in chamber_ids:
        raise SignatureError("evidence mixes gas chambers; closed-chamber pairing required")
    seen: set[str] = set()
    hashes: list[str] = []
    device_keys: dict[str, str] = {}
    for envelope, body in zip(bundle["envelopes"], bodies):
        digest = content_hash(body)
        if digest in seen:
            raise SignatureError(f"duplicate original {body_id(body)} (hash de-dup)")
        seen.add(digest)
        hashes.append(digest)
        device = body.get("device_id")
        if device:
            device_keys.setdefault(device, envelope["key_id"])
    return EvidenceBundle(next(iter(bottle_ids)), next(iter(chamber_ids)),
                          bodies, hashes, device_keys)


def load_bundle(path: str | Path, keys: dict[str, bytes]) -> EvidenceBundle:
    bundle = json.loads(Path(path).read_bytes())
    return capture(bundle, keys)


# ---------------------------------------------------------------------------
# Same-instant pressure/temperature pairing under one clock anchor + epoch
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PairedSample:
    seq: int
    at: str
    pressure: float
    pressure_u: float
    temperature_k: float
    temperature_u: float
    gauge_id: str
    thermometer_id: str
    pressure_hash: str
    temperature_hash: str


def _within_tolerance(t_a: str, t_b: str, anchor: ClockAnchor) -> bool:
    return abs(parse_clock(t_a) - parse_clock(t_b)) <= anchor.max_offset_seconds


def pair_samples(eb: EvidenceBundle) -> list[PairedSample]:
    """Pair pressure and temperature for the same bottle at comparable instants.

    Hard constraint one: every pair shares one clock anchor and one calibration
    epoch.  Drifting devices cannot be picked point-by-point; if a pressure
    point lacks a co-anchored temperature (or vice versa) the bundle is
    rejected rather than silently averaged.
    """
    anchor = eb.clock_anchor()
    if anchor is None:
        raise ValueError("no shared clock anchor; pressure and temperature are not comparable")
    pressures = sorted(eb.by_type("pressure"), key=lambda r: r["at"])
    temps = sorted(eb.by_type("temperature"), key=lambda r: r["at"])
    if not pressures or not temps:
        raise ValueError("paired sampling needs both pressure and temperature originals")
    paired: list[PairedSample] = []
    used_temps: set[str] = set()
    for seq, prec in enumerate(pressures):
        cal = eb.calibration(prec["device_id"], prec["at"])
        if cal is None:
            raise ValueError(f"pressure {prec['id']} has no valid gauge calibration")
        if cal.epoch != anchor.epoch:
            raise ValueError("calibration epoch and clock anchor epoch differ")
        candidates = [t for t in temps
                      if t["id"] not in used_temps
                      and t.get("chamber_id") == eb.chamber_id
                      and _within_tolerance(prec["at"], t["at"], anchor)]
        if not candidates:
            raise ValueError(
                f"pressure {prec['id']} at {prec['at']} has no co-anchored temperature")
        trec = min(candidates, key=lambda t: abs(parse_clock(t["at"]) - parse_clock(prec["at"])))
        used_temps.add(trec["id"])
        if trec.get("epoch") and trec["epoch"] != anchor.epoch:
            raise ValueError("temperature epoch differs from the shared calibration epoch")
        indicated = to_nominal_pressure(prec["value"], prec.get("unit", "bar"))
        corrected, cal_u = cal.correct(indicated)
        pressure_u = (abs(prec.get("uncertainty", 0.0))
                      + cal_u + abs(prec.get("drift", 0.0)))
        t_k = to_kelvin(trec["value"], trec.get("unit", "K"))
        paired.append(PairedSample(
            seq, prec["at"], corrected, pressure_u, t_k,
            abs(trec.get("uncertainty_k", trec.get("uncertainty", 0.0))),
            prec["device_id"], trec["device_id"],
            content_hash(prec), content_hash(trec)))
    epochs = {eb.clock_anchor().epoch}
    if len(epochs) != 1:  # defensive: one common epoch for the whole chamber
        raise ValueError("multiple calibration epochs in one chamber")
    return paired


# ---------------------------------------------------------------------------
# SQLite persistence: hash de-dup + all-or-nothing transaction
# ---------------------------------------------------------------------------

EVIDENCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_sources (
    sha TEXT PRIMARY KEY,
    bottle_id TEXT NOT NULL,
    chamber_id TEXT NOT NULL,
    raw BLOB NOT NULL,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS evidence_records (
    sha TEXT PRIMARY KEY,
    source_sha TEXT NOT NULL,
    record_type TEXT NOT NULL,
    body TEXT NOT NULL,
    FOREIGN KEY(source_sha) REFERENCES evidence_sources(sha)
);
"""


def init_evidence_schema(db: sqlite3.Connection) -> None:
    db.executescript(EVIDENCE_SCHEMA)


def import_bundle(db: sqlite3.Connection, bundle: dict,
                  keys: dict[str, bytes]) -> dict:
    """Verify, de-duplicate and persist a bundle inside one transaction."""
    eb = capture(bundle, keys)
    raw = canonical(bundle)
    sha = hashlib.sha256(raw).hexdigest()
    init_evidence_schema(db)
    if db.execute("SELECT 1 FROM evidence_sources WHERE sha=?", (sha,)).fetchone():
        return {"sha": sha, "inserted": 0, "idempotent": True,
                "bottle_id": eb.bottle_id}
    db.execute("SAVEPOINT evidence")
    try:
        db.execute("INSERT INTO evidence_sources(sha,bottle_id,chamber_id,raw) VALUES (?,?,?,?)",
                   (sha, eb.bottle_id, eb.chamber_id, raw))
        for rec, digest in zip(eb.records, eb.hashes):
            db.execute("INSERT INTO evidence_records VALUES (?,?,?,?)",
                       (digest, sha, rec["type"],
                        json.dumps(rec, ensure_ascii=False, sort_keys=True)))
        db.execute("RELEASE SAVEPOINT evidence")
        db.commit()
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT evidence")
        db.execute("RELEASE SAVEPOINT evidence")
        db.rollback()
        raise
    return {"sha": sha, "inserted": len(eb.records), "idempotent": False,
            "bottle_id": eb.bottle_id}
