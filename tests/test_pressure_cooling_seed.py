"""Cooling-vs-leak triage tests for returned cylinder CYL-1.

Covers the seeded mis-isolation, both authorised evidence routes (cooling
release / leak extension), temperature mismatch rejection, gauge calibration
drift witnesses, old-order readback and SQLite transactional capture.
"""
import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from cylindertrail.pressure import drop, cooling_expected_pressure, joint_feasible_states
from cylindertrail.pressure_capture import (
    SignatureError, apply_uncertainty, capture, import_bundle,
    load_bundle, pair_samples, to_kelvin, to_nominal_pressure,
)
from cylindertrail.leak_triage import (
    TriageLedger, adjacent_bays, adjudicate, status, triage_bundle,
)
from cylindertrail.rules import return_handoff_state
from cylindertrail.store import connect, import_file

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "fixtures" / "evidence"


def _keys():
    raw = json.loads((EVIDENCE / "keys.json").read_text(encoding="utf-8"))
    return {k: v.encode("utf-8") for k, v in raw.items()}


def _bundle(name):
    if not name.endswith(".json"):
        name += ".json"
    return json.loads((EVIDENCE / name).read_text(encoding="utf-8"))


def _handoff(returned=True, contaminated=False):
    events = [{
        "id": "D3", "kind": "dispatch", "bottle_id": "CYL-1", "version": 3,
        "effective_at": "2026-11-24T12:00",
        "outbound": {"station_signature": "ST1", "customer_signature": "CU1"},
        "return": ({"station_signature": "ST1", "residual_pressure": 90,
                    "max_residual_pressure": 95,
                    "contaminated": contaminated} if returned else None),
        "recall_notice": {},
    }]
    return return_handoff_state(events, "CYL-1")


# ---------------------------------------------------------------------------
# Seed: the raw pressure bytes cannot separate cooling from leakage
# ---------------------------------------------------------------------------

def test_pressure_drop_cannot_separate_cooling_and_leak():
    d = json.loads((ROOT / "fixtures" / "pressure-cooling.json").read_text())
    assert drop(d["readings"]) == 10
    assert status(d["readings"]) == "isolate"
    # Constant mass, fixed volume, ideal gas: 100 bar at 300 K -> 90 at 270 K.
    assert cooling_expected_pressure(100, 300, 270) == 90


def test_no_temperature_keeps_both_worlds_feasible_and_blocks_release():
    feasible = joint_feasible_states([])
    assert feasible["worlds"]["cooling"]["feasible"] is True
    assert feasible["worlds"]["leak"]["feasible"] is True
    verdict = adjudicate(feasible)
    assert verdict["disposition"] == "isolate"
    assert verdict["storage_bay_class"] == "isolation_bay"


# ---------------------------------------------------------------------------
# Route A: sealed, 300 -> 270 K calibrated, mass holds, detector negative
# ---------------------------------------------------------------------------

def test_route_a_cooling_releases_and_preserves_original_isolation():
    eb = load_bundle(EVIDENCE / "route-a-cooling.json", _keys())
    result = triage_bundle(eb, handoff=_handoff())
    worlds = result["feasible"]["worlds"]
    assert worlds["cooling"]["feasible"] is True
    assert worlds["leak"]["feasible"] is False
    assert worlds["leak"]["witness"] is not None
    assert result["feasible"]["mass_balance"]["mass_constant"] is True
    assert result["feasible"]["zone_detector"]["positive"] is False
    assert result["verdict"]["disposition"] == "release"
    assert result["verdict"]["storage_bay_class"] == "routine_bay"

    ledger = TriageLedger()
    order = ledger.open_isolation(
        "CYL-1", "raw drop 100->90 exceeds threshold 5 without temperature record",
        zone="Z-A", bay="A1").as_dict()
    assert order["version"] == 1 and order["disposition"] == "isolate"
    revised = ledger.revise("CYL-1", {**result["verdict"],
                                      "basis_hashes": eb.hashes},
                            zone="Z-A", bay="A1").as_dict()
    assert revised["version"] == 2 and revised["disposition"] == "release"
    assert revised["supersedes"] == 1
    # Original isolate version and its rationale remain readable.
    first = ledger.history("CYL-1")[0]
    assert first["disposition"] == "isolate"
    assert "threshold 5" in first["reason"]
    assert revised["prior_rationale"] == first["reason"]
    # Negative detector originals close the carried zone linkage explicitly.
    assert all(a["status"] == "closed" for a in revised["zone_actions"])
    assert ledger.open_zone_actions("Z-A") == []


def test_route_a_release_gate_joins_return_handoff_state():
    eb = load_bundle(EVIDENCE / "route-a-cooling.json", _keys())
    result = triage_bundle(eb, handoff=_handoff(returned=False))
    assert result["verdict"]["disposition"] == "pending_verification"
    assert "station custody" in " ".join(result["verdict"]["handoff_gate"]["blockers"])

    contaminated = triage_bundle(eb, handoff=_handoff(contaminated=True))
    assert contaminated["verdict"]["disposition"] == "pending_verification"
    assert contaminated["verdict"]["handoff_gate"]["release_allowed"] is False


# ---------------------------------------------------------------------------
# Route B: constant 300 K, weight loss, positive zone detector
# ---------------------------------------------------------------------------

def test_route_b_leak_keeps_isolation_and_extends_to_adjacent_bays():
    eb = load_bundle(EVIDENCE / "route-b-leak.json", _keys())
    result = triage_bundle(eb, handoff=_handoff())
    worlds = result["feasible"]["worlds"]
    assert worlds["cooling"]["feasible"] is False
    assert worlds["cooling"]["witness"] is not None
    assert worlds["leak"]["feasible"] is True
    assert result["feasible"]["mass_balance"]["mass_lost"] is True
    assert result["feasible"]["zone_detector"]["positive"] is True
    assert result["verdict"]["disposition"] == "isolate"
    assert "adjacent bays" in result["verdict"]["reason"]

    ledger = TriageLedger()
    ledger.open_isolation("CYL-1", "initial raw-drop isolation", zone="Z-A", bay="A1")
    revised = ledger.revise("CYL-1", {**result["verdict"],
                                      "basis_hashes": eb.hashes},
                            zone="Z-A", bay="A1").as_dict()
    assert revised["disposition"] == "isolate"
    scopes = {tuple(a["scope"]) for a in revised["zone_actions"] if a["status"] == "open"}
    # Suspect bay plus every adjacent bay in zone Z-A is under open inspection.
    assert any("A1" in s and set(s) >= {"A1", "A2", "A3"} for s in scopes)
    assert ledger.open_zone_actions("Z-A"), "zone linkage must not be silently erased"
    assert adjacent_bays("Z-A", "A1") == ["A2", "A3"]


def test_two_routes_share_pressure_bytes_but_diverge_on_storage():
    a = triage_bundle(load_bundle(EVIDENCE / "route-a-cooling.json", _keys()),
                      handoff=_handoff())
    b = triage_bundle(load_bundle(EVIDENCE / "route-b-leak.json", _keys()),
                      handoff=_handoff())
    pa = [s["pressure"] for s in a["feasible"]["bottle_states"]]
    pb = [s["pressure"] for s in b["feasible"]["bottle_states"]]
    assert pa == pb == [100.0, 90.0]
    assert a["verdict"]["storage_bay_class"] == "routine_bay"
    assert b["verdict"]["storage_bay_class"] == "isolation_bay"


# ---------------------------------------------------------------------------
# Temperature mismatch and calibration drift
# ---------------------------------------------------------------------------

def test_temperature_from_another_bottle_is_rejected_not_configured():
    with pytest.raises(SignatureError, match="same-bottle|chamber"):
        capture(_bundle("route-mismatch"), _keys())
    # Sensor trust is key/evidence based: an unregistered key cannot flip it on.
    tampered = _bundle("route-a-cooling.json")
    tampered["envelopes"][2]["sig"] = "0" * 64
    with pytest.raises(SignatureError):
        capture(tampered, _keys())
    with pytest.raises(SignatureError):
        capture(_bundle("route-a-cooling.json"), {"key-gauge-g1": b"wrong-key"})


def test_calibration_drift_witness_excludes_gauge_world_but_keeps_leak():
    eb = load_bundle(EVIDENCE / "route-gauge-drift.json", _keys())
    result = triage_bundle(eb, handoff=_handoff())
    worlds = result["feasible"]["worlds"]
    assert worlds["gauge_drift"]["feasible"] is False
    witness = worlds["gauge_drift"]["witness"]
    assert witness["constraint"] == "corrected pressure intervals overlap"
    # The two corrected intervals are disjoint with a positive gap.
    p1_lo, p1_hi = witness["expected_interval"]
    p2_lo, p2_hi = witness["observed_interval"]
    assert p2_hi < p1_lo and p1_hi > p2_lo
    assert worlds["leak"]["feasible"] is True
    assert result["verdict"]["disposition"] == "isolate"


# ---------------------------------------------------------------------------
# Revisions touch only CYL-1; old orders and unrelated bottles stay readable
# ---------------------------------------------------------------------------

def test_revision_only_changes_target_bottle_and_keeps_unrelated_state():
    ledger = TriageLedger()
    ledger.open_isolation("CYL-1", "initial order", zone="Z-A", bay="A1")
    ledger.open_isolation("CYL-2", "routine returned-bottle check", zone="Z-B", bay="B1")
    eb = load_bundle(EVIDENCE / "route-a-cooling.json", _keys())
    verdict = triage_bundle(eb, handoff=_handoff())["verdict"]
    ledger.revise("CYL-1", {**verdict, "basis_hashes": eb.hashes}, zone="Z-A", bay="A1")
    assert ledger.current("CYL-1")["disposition"] == "release"
    other = ledger.current("CYL-2")
    assert other["disposition"] == "isolate" and other["version"] == 1
    # Old-order readback: every revision remains auditable.
    history = ledger.history("CYL-1")
    assert [h["version"] for h in history] == [1, 2]
    assert history[0]["disposition"] == "isolate"
    with pytest.raises(KeyError):
        ledger.revise("CYL-9", verdict)


# ---------------------------------------------------------------------------
# Unit conversion, hashing, idempotent transactional SQLite capture
# ---------------------------------------------------------------------------

def test_unit_conversion_and_uncertainty_intervals():
    assert to_kelvin(-3.15, "C") == pytest.approx(270.0)
    assert to_kelvin(300, "K") == 300.0
    assert to_nominal_pressure(1000, "kPa") == pytest.approx(10.0)
    assert to_nominal_pressure(14.5037738, "psi") == pytest.approx(1.0, abs=1e-6)
    assert apply_uncertainty(90.0, 0.5) == (89.5, 90.5)


def test_bundle_import_is_idempotent_and_atomic_in_sqlite():
    db = sqlite3.connect(":memory:")
    first = import_bundle(db, _bundle("route-a-cooling.json"), _keys())
    assert first["inserted"] == 10 and first["idempotent"] is False
    again = import_bundle(db, _bundle("route-a-cooling.json"), _keys())
    assert again["idempotent"] is True and again["inserted"] == 0
    rows = db.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0]
    assert rows == 10
    # Tampered bundle rolls back: no partial rows survive.
    tampered = _bundle("route-b-leak.json")
    tampered["envelopes"][0]["sig"] = "deadbeef"
    with pytest.raises(SignatureError):
        import_bundle(db, tampered, _keys())
    assert db.execute("SELECT COUNT(*) FROM evidence_sources").fetchone()[0] == 1


def test_hash_dedup_rejects_replayed_original():
    bundle = _bundle("route-a-cooling.json")
    bundle["envelopes"].append(bundle["envelopes"][2])  # replay one pressure original
    with pytest.raises(SignatureError, match="duplicate"):
        capture(bundle, _keys())


def test_pressure_temperature_must_share_clock_anchor_and_epoch():
    bundle = _bundle("route-a-cooling.json")
    from cylindertrail.pressure_capture import sign
    keys = _keys()
    # Tighten the shared anchor to zero tolerance, then shift both temperature
    # instants away from the pressure instants.
    for env in bundle["envelopes"]:
        body = env["body"]
        if body.get("type") == "clock_anchor":
            body["max_offset_seconds"] = 0
        if body.get("type") == "temperature":
            body["at"] = body["at"].replace("09:00", "09:05").replace("10:00", "10:05")
        env["sig"] = sign(body, keys[env["key_id"]])
    eb = capture(bundle, keys)
    with pytest.raises(ValueError, match="co-anchored"):
        pair_samples(eb)


def test_triage_runs_alongside_existing_return_handoff_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        db = connect(Path(tmp) / "state.sqlite")
        import_file(db, "dispatch", ROOT / "fixtures" / "dispatch-ok.json")
        # dispatch-ok has no signed return yet: custody stays with the customer.
        events = [json.loads(row[0]) for row in db.execute("SELECT body FROM events")]
        handoff = return_handoff_state(events, "CYL-1")
        assert handoff["custody"] == "customer" and handoff["returned"] is False
        eb = load_bundle(EVIDENCE / "route-a-cooling.json", _keys())
        verdict = triage_bundle(eb, handoff=handoff)["verdict"]
        assert verdict["disposition"] == "pending_verification"
