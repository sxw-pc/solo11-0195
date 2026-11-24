"""Per-bottle leakage triage and storage-bay disposition.

The seeded :func:`status` keeps the original threshold behaviour (the starting
isolation order is real and must remain readable).  New evidence never edits
that order in place: :class:`TriageLedger` appends a versioned disposition that
keeps the original isolate rationale, changes only CYL-1's own record, and
carries storage-zone linkage actions forward until negative detector evidence
closes them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .pressure import drop, joint_feasible_states
from .pressure_capture import EvidenceBundle, pair_samples

# Storage layout: adjacent bays within a zone share ventilation/detector risk.
ZONE_BAYS = {"Z-A": ["A1", "A2", "A3"], "Z-B": ["B1", "B2"], "Z-C": ["C1", "C2"]}


def status(readings):
    """Seed-compatible threshold call on raw pressure-only readings."""
    return 'isolate' if drop(readings) > 5 else 'normal_storage'


def adjacent_bays(zone: str, bay: str) -> list[str]:
    bays = ZONE_BAYS.get(zone, [])
    return [b for b in bays if b != bay]


# ---------------------------------------------------------------------------
# World-level adjudication over the joint feasible-state report
# ---------------------------------------------------------------------------

def adjudicate(feasible: dict[str, Any], *, handoff: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn the feasible worlds + mass/detector evidence into a disposition.

    Three feasible worlds are listed together; release requires the leak world
    excluded *and* corroborating constant-mass plus negative detector originals.
    A single temperature average can never auto-release the bottle.
    """
    worlds = feasible["worlds"]
    mass = feasible["mass_balance"]
    detector = feasible["zone_detector"]
    cooling_only = worlds["cooling"]["feasible"] and not worlds["leak"]["feasible"]
    leak_corroborated = (mass.get("available") and mass.get("mass_lost")) or \
                        (detector.get("available") and detector.get("positive"))
    mass_holds = mass.get("available") and mass.get("mass_constant")
    detector_clear = detector.get("available") and not detector.get("positive")
    only_drift_left = (worlds["gauge_drift"]["feasible"]
                       and not worlds["cooling"]["feasible"]
                       and not worlds["leak"]["feasible"])

    if not feasible["bottle_states"]:
        disposition, reason = "isolate", (
            "no trustworthy co-anchored temperature: 100->90 fits cooling and leak alike; "
            "hold isolation pending verification")
    elif cooling_only and mass_holds and detector_clear:
        disposition, reason = "release", (
            "leak world excluded within error bounds; sealed mass unchanged and zone detector "
            "negative; cooling 300->270 K explains 100->90")
    elif leak_corroborated:
        disposition, reason = "isolate", (
            "constant-temperature mass loss and/or positive zone gas detection corroborate leakage; "
            "extend checks to adjacent bays")
    elif only_drift_left:
        disposition, reason = "pending_verification", (
            "only gauge-drift world survives; send the gauge for recalibration before disposition")
    elif worlds["leak"]["feasible"]:
        disposition, reason = "pending_verification", (
            "leak world not excluded within measurement error bounds; keep off routine storage")
    else:
        disposition, reason = "pending_verification", "inconclusive joint evidence; manual review"

    storage = {
        "release": "routine_bay",
        "isolate": "isolation_bay",
        "pending_verification": "quarantine_bay",
    }[disposition]

    # The release gate joins the return-handoff state from rules.py: the bottle
    # must physically be back under station custody and not contaminated.
    handoff_gate = {"checked": handoff is not None, "release_allowed": True,
                    "custody": None, "blockers": []}
    if handoff is not None:
        handoff_gate["custody"] = handoff.get("custody")
        if handoff.get("custody") != "station":
            handoff_gate["release_allowed"] = False
            handoff_gate["blockers"].append("bottle not back under station custody")
        if handoff.get("contaminated"):
            handoff_gate["release_allowed"] = False
            handoff_gate["blockers"].append("return receipt marks contamination / excess residual pressure")
    if disposition == "release" and not handoff_gate["release_allowed"]:
        disposition, storage = "pending_verification", "quarantine_bay"
        reason += "; handoff gate closed: " + ", ".join(handoff_gate["blockers"])

    return {"disposition": disposition, "storage_bay_class": storage,
            "reason": reason, "handoff_gate": handoff_gate,
            "feasible_worlds": {k: v["feasible"] for k, v in worlds.items()},
            "conflict_witnesses": [v["witness"] for v in worlds.values() if v["witness"]],
            "mass_balance": mass, "zone_detector": detector}


def triage_bundle(eb: EvidenceBundle, *, handoff: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full pipeline: pair verified originals, enumerate worlds, adjudicate."""
    samples = pair_samples(eb)
    feasible = joint_feasible_states(
        samples, mass_readings=eb.by_type("mass"),
        detector_readings=eb.by_type("gas_detector"))
    verdict = adjudicate(feasible, handoff=handoff)
    return {"bottle_id": eb.bottle_id, "chamber_id": eb.chamber_id,
            "evidence_hashes": eb.hashes, "feasible": feasible, "verdict": verdict}


# ---------------------------------------------------------------------------
# Versioned triage ledger: append-only, per bottle, zone linkage preserved
# ---------------------------------------------------------------------------

@dataclass
class TriageOrder:
    bottle_id: str
    version: int
    disposition: str
    storage_bay_class: str
    reason: str
    basis_hashes: list[str]
    supersedes: int | None
    zone_actions: list[dict[str, Any]] = field(default_factory=list)
    prior_rationale: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"bottle_id": self.bottle_id, "version": self.version,
                "disposition": self.disposition,
                "storage_bay_class": self.storage_bay_class, "reason": self.reason,
                "basis_hashes": list(self.basis_hashes),
                "supersedes": self.supersedes,
                "zone_actions": list(self.zone_actions),
                "prior_rationale": self.prior_rationale}


class TriageLedger:
    """Append-only disposition history.

    Late evidence appends a new version for exactly one bottle; unrelated
    bottles keep their current state.  Zone linkage created by a suspected
    leak is carried into later versions and can only be closed by explicit
    negative detector evidence -- never silently erased by a revision.
    """

    def __init__(self) -> None:
        self._orders: dict[str, list[TriageOrder]] = {}

    def open_isolation(self, bottle_id: str, reason: str, *,
                       zone: str | None = None, bay: str | None = None,
                       basis_hashes: Sequence[str] | None = None) -> TriageOrder:
        if bottle_id in self._orders:
            raise ValueError(f"{bottle_id} already has a triage history; revise instead")
        zone_actions = []
        if zone:
            scope = ([bay] if bay else ["suspect_bay"]) + \
                    (adjacent_bays(zone, bay) if bay else [])
            zone_actions.append({"zone": zone, "scope": scope,
                                 "action": "inspect", "status": "open",
                                 "basis": "initial isolation order"})
        order = TriageOrder(bottle_id, 1, "isolate", "isolation_bay", reason,
                            list(basis_hashes or []), None, zone_actions)
        self._orders[bottle_id] = [order]
        return order

    def revise(self, bottle_id: str, verdict: dict[str, Any], *,
               zone: str | None = None, bay: str | None = None) -> TriageOrder:
        history = self._orders.get(bottle_id)
        if not history:
            raise KeyError(f"no prior order for {bottle_id}; open_isolation first")
        previous = history[-1]
        carried = [dict(a) for a in previous.zone_actions if a.get("status") == "open"]
        detector = verdict.get("zone_detector", {})
        # Explicit negative detector originals are the only thing that closes linkage.
        if detector.get("available") and not detector.get("positive"):
            for action in carried:
                action["status"] = "closed"
                action["closure_basis"] = "negative continuous zone gas detection originals"
        if verdict["disposition"] == "isolate" and zone:
            scope = ([bay] if bay else ["suspect_bay"]) + \
                    (adjacent_bays(zone, bay) if bay else [])
            carried.append({"zone": zone, "scope": scope,
                            "action": "inspect", "status": "open",
                            "basis": verdict["reason"]})
        order = TriageOrder(
            bottle_id, previous.version + 1, verdict["disposition"],
            verdict["storage_bay_class"], verdict["reason"],
            verdict.get("basis_hashes", []), previous.version, carried,
            prior_rationale=previous.reason)
        history.append(order)
        return order

    def history(self, bottle_id: str) -> list[dict[str, Any]]:
        return [o.as_dict() for o in self._orders.get(bottle_id, [])]

    def current(self, bottle_id: str) -> dict[str, Any] | None:
        history = self._orders.get(bottle_id)
        return history[-1].as_dict() if history else None

    def open_zone_actions(self, zone: str) -> list[dict[str, Any]]:
        out = []
        for history in self._orders.values():
            for action in history[-1].zone_actions:
                if action.get("zone") == zone and action.get("status") == "open":
                    out.append({"bottle_id": history[-1].bottle_id, **action})
        return out
