"""Append-only capture ledger for operator qualification evidence.

This is the "逐介质授权台账" (per-medium authority ledger).  It captures, in
order and without mutation:

* training segments (instructor / sign-in / departure / course validity),
* practical equipment examinations,
* external reciprocal credentials and issuer trust material,
* shift schedules and completed fills,
* late revocations / exam reviews / issuer invalidations.

Only future (not-yet-executed) shift authorizations are withdrawn by
:meth:`QualificationLedger.reconcile_shifts`.  Completed fills keep the basis
snapshot captured when they happened; old events are never edited or deleted,
so later evidence cannot rewrite history into "the operator never worked".
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional

from . import operator_authority as oa
from . import training_credit as tc

__all__ = [
    "QualificationLedger", "Ledger", "AuthorityLedger", "OperatorLedger",
    "QualificationCapture", "CaptureLedger",
    "capture_event", "ledger_from_fixture", "load_fixture",
    "build_ledger",
]

_USE_DEFAULT = object()

# Aliases accepted on ingest -> canonical event kind.
_KIND_ALIASES = {
    "session": "training_session",
    "training": "training_session",
    "training_session": "training_session",
    "course_session": "training_session",
    "class_session": "training_session",
    "session_revocation": "session_revocation",
    "revoke_session": "session_revocation",
    "training_revocation": "session_revocation",
    "signin_revocation": "session_revocation",
    "practical_exam": "practical_exam",
    "exam": "practical_exam",
    "practical": "practical_exam",
    "assessment": "practical_exam",
    "equipment_exam": "practical_exam",
    "exam_review": "exam_review",
    "exam_revocation": "exam_review",
    "exam_invalidation": "exam_review",
    "credential": "reciprocal_credential",
    "reciprocal_credential": "reciprocal_credential",
    "external_credential": "reciprocal_credential",
    "recognition": "reciprocal_credential",
    "credential_revocation": "credential_revocation",
    "revoke_credential": "credential_revocation",
    "issuer_invalidation": "issuer_invalidation",
    "issuer_revocation": "issuer_invalidation",
    "invalidate_issuer": "issuer_invalidation",
    "identity_verification": "identity_verification",
    "identity_check": "identity_verification",
    "shift": "shift_scheduled",
    "shift_scheduled": "shift_scheduled",
    "schedule_shift": "shift_scheduled",
    "shift_authorization_revoked": "shift_authorization_revoked",
    "shift_revoked": "shift_authorization_revoked",
    "fill": "fill_completed",
    "fill_completed": "fill_completed",
    "completed_fill": "fill_completed",
    "inspection": "inspection_signed",
    "inspection_signed": "inspection_signed",
    "inspection_signature": "inspection_signed",
}


def _canonical_kind(event: dict) -> str:
    raw = str(event.get("kind") or event.get("type") or event.get("event") or "").strip()
    key = raw.lower().replace("-", "_").replace(" ", "_")
    return _KIND_ALIASES.get(key, raw or "event")


class QualificationLedger:
    """Ordered, append-only evidence/authority ledger for one station."""

    def __init__(self, fixture: Any = None, *, site: Optional[str] = None,
                 site_rule: Optional[dict] = None,
                 minimum_hours: Optional[float] = None,
                 media: Optional[list[str]] = None,
                 operators: Optional[list[str]] = None,
                 instructors: Optional[dict] = None,
                 courses: Optional[dict] = None,
                 examiners: Optional[dict] = None,
                 inspectors: Optional[dict] = None,
                 issuers: Optional[dict] = None):
        fixture_data: dict = {}
        if fixture is not None:
            fixture_data = (_load_json(fixture) if not isinstance(fixture, dict)
                            else fixture)
        site_block = fixture_data.get("site")
        fixture_site = site_block.get("id") if isinstance(site_block, dict) else site_block
        self.site = site or fixture_site or "FILL-STATION-1"
        rule = dict(oa.DEFAULT_RULE)
        rule["segment_proof_required"] = True  # hard constraint one, enforced here
        fixture_rule = fixture_data.get("site_rule") if isinstance(
            fixture_data.get("site_rule"), dict) else {}
        rule.update(fixture_rule or {})
        if site_rule:
            rule.update(site_rule)
        if minimum_hours is not None:
            rule["minimum_nonoverlap_hours"] = float(minimum_hours)
        else:
            rule.setdefault("minimum_nonoverlap_hours", 40.0)
        self.site_rule = rule
        self.media = media or (fixture_rule or {}).get("media") or ["O2", "N2"]
        fixture_ops = ([fixture_data["operator"]["id"]]
                       if isinstance(fixture_data.get("operator"), dict) else [])
        self.known_operators = list(operators if operators is not None else fixture_ops)
        self.instructors = dict(instructors or fixture_data.get("instructors") or {})
        self.courses = dict(courses or fixture_data.get("courses") or {})
        self.examiners = dict(examiners or fixture_data.get("examiners") or {})
        self.inspectors = dict(inspectors or fixture_data.get("inspectors") or {})
        self.issuers = dict(issuers or fixture_data.get("issuers") or {})
        self._events: list[dict] = []
        self._seq = 0
        if fixture_data.get("ledger_events"):
            self.capture_many(fixture_data["ledger_events"])

    @classmethod
    def from_fixture(cls, source: Any) -> "QualificationLedger":
        """Create a ledger from a fixture dict/path and replay its events."""
        return ledger_from_fixture(source)

    # ------------------------------------------------------------------ #
    # Append-only capture
    # ------------------------------------------------------------------ #
    @property
    def events(self) -> list[dict]:
        return copy.deepcopy(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def capture(self, event: Optional[dict] = None, **fields: Any) -> dict:
        """Append one evidence event. Input is deep-copied; returns stored row."""
        if event is None:
            event = fields
        elif fields:
            event = {**event, **fields}
        if not isinstance(event, dict):
            raise TypeError("ledger events must be dicts")
        stored = copy.deepcopy(event)
        kind = _canonical_kind(stored)
        if stored.get("kind") and kind != stored["kind"]:
            stored["source_kind"] = stored["kind"]
        stored["kind"] = kind
        self._seq += 1
        stored["seq"] = self._seq
        if "id" not in stored or stored["id"] is None:
            stored["id"] = f"E{self._seq:04d}"

        if kind == "fill_completed" and "basis" not in stored:
            self._attach_fill_basis(stored)
        if kind == "shift_scheduled" and "authorization_snapshot" not in stored:
            self._attach_shift_snapshot(stored)

        self._events.append(stored)
        return copy.deepcopy(stored)

    record = capture
    add = capture

    def capture_many(self, events: list[dict]) -> list[dict]:
        return [self.capture(event) for event in events]

    def _attach_fill_basis(self, stored: dict) -> None:
        operator = tc.first_present(stored, "operator", "operator_id", "filled_by")
        medium = _medium(stored.get("medium"))
        when = tc.first_present(stored, "filled_at", "at", "completed_at", "timestamp")
        decision = oa.evaluate(self.authority_bundle(), person=operator,
                                at=when, knowledge_at=when)
        stored["basis"] = decision.as_dict()
        stored["permitted"] = bool(medium and decision.for_medium(medium) == oa.AUTHORIZED)

    def _attach_shift_snapshot(self, stored: dict) -> None:
        operator = tc.first_present(stored, "operator", "operator_id", default=None)
        start = tc.first_present(stored, "start", "starts_at", "begin", default=None)
        media = _media_of(stored)
        snapshot = {}
        permitted = True
        for medium in media:
            d = oa.evaluate(self.authority_bundle(), person=operator,
                            medium=medium, at=start, knowledge_at=start)
            snapshot[medium] = d.status
            permitted = permitted and d.status == oa.AUTHORIZED
        stored["authorization_snapshot"] = snapshot
        stored["initially_authorized"] = permitted

    # ------------------------------------------------------------------ #
    # Typed capture helpers
    # ------------------------------------------------------------------ #
    def add_training_session(self, session: Optional[dict] = None, **kw: Any) -> dict:
        payload = dict(session or {})
        payload.update(kw)
        payload.setdefault("kind", "training_session")
        return self.capture(payload)

    def revoke_training(self, target_id: str, *, at: Any = None,
                        reason: str = "签到撤销", revocation_id: Optional[str] = None,
                        **kw: Any) -> dict:
        return self.capture({"kind": "session_revocation",
                             "id": revocation_id or f"RV-SESSION-{target_id}",
                             "target": target_id, "at": at, "reason": reason, **kw})

    def record_exam(self, exam: Optional[dict] = None, **kw: Any) -> dict:
        payload = dict(exam or {})
        payload.update(kw)
        payload.setdefault("kind", "practical_exam")
        return self.capture(payload)

    def review_exam(self, target_id: str, *, passed: bool = False, at: Any = None,
                    reason: str = "考试复核改判", review_id: Optional[str] = None,
                    **kw: Any) -> dict:
        return self.capture({"kind": "exam_review",
                             "id": review_id or f"RV-EXAM-{target_id}",
                             "target": target_id, "passed": passed, "at": at,
                             "result": "failed" if not passed else "upheld",
                             "reason": reason, **kw})

    def add_reciprocal_credential(self, cred: Optional[dict] = None, **kw: Any) -> dict:
        payload = dict(cred or {})
        payload.update(kw)
        payload.setdefault("kind", "reciprocal_credential")
        return self.capture(payload)

    def revoke_credential(self, target_id: str, *, at: Any = None,
                          reason: str = "互认撤销", revocation_id: Optional[str] = None,
                          **kw: Any) -> dict:
        return self.capture({"kind": "credential_revocation",
                             "id": revocation_id or f"RV-CRED-{target_id}",
                             "target": target_id, "at": at, "reason": reason, **kw})

    def invalidate_issuer(self, issuer_id: str, *, at: Any = None,
                          reason: str = "发证机构失效",
                          invalidation_id: Optional[str] = None, **kw: Any) -> dict:
        return self.capture({"kind": "issuer_invalidation",
                             "id": invalidation_id or f"RV-ISSUER-{issuer_id}",
                             "issuer": issuer_id, "target": issuer_id,
                             "target_type": "issuer", "at": at, "reason": reason, **kw})

    def verify_identity(self, person: str, *, verified: bool = True, at: Any = None,
                        credential_id: Optional[str] = None, **kw: Any) -> dict:
        return self.capture({"kind": "identity_verification", "operator": person,
                             "verified": verified, "at": at,
                             "credential_id": credential_id, **kw})

    def schedule_shift(self, shift: Optional[dict] = None, **kw: Any) -> dict:
        payload = dict(shift or {})
        payload.update(kw)
        payload.setdefault("kind", "shift_scheduled")
        payload.setdefault("status", "scheduled")
        return self.capture(payload)

    def complete_fill(self, fill: Optional[dict] = None, **kw: Any) -> dict:
        payload = dict(fill or {})
        payload.update(kw)
        payload.setdefault("kind", "fill_completed")
        payload.setdefault("status", "completed")
        return self.capture(payload)

    def record_inspection(self, inspection: Optional[dict] = None, **kw: Any) -> dict:
        payload = dict(inspection or {})
        payload.update(kw)
        payload.setdefault("kind", "inspection_signed")
        return self.capture(payload)

    # ------------------------------------------------------------------ #
    # Bundle / queries
    # ------------------------------------------------------------------ #
    def authority_bundle(self) -> dict:
        """Plain evidence mapping consumed by operator_authority.normalize_bundle."""
        sessions, exams, credentials, revocations = [], [], [], []
        fills, inspections, shifts = [], [], []
        for event in self._events:
            kind = event.get("kind", "")
            if kind == "training_session":
                sessions.append(event)
            elif kind == "practical_exam":
                exams.append(event)
            elif kind == "reciprocal_credential":
                credentials.append(event)
            elif kind in {"session_revocation", "exam_review",
                          "credential_revocation", "issuer_invalidation"}:
                revocations.append(event)
            elif kind == "fill_completed":
                fills.append(event)
            elif kind == "inspection_signed":
                inspections.append(event)
            elif kind == "shift_scheduled":
                shifts.append(event)
        return {
            "site": self.site,
            "rule": self.site_rule,
            "media": self.media,
            "requested_media": self.media,
            "instructors": self.instructors,
            "courses": self.courses,
            "examiners": self.examiners,
            "inspectors": self.inspectors,
            "issuers": self.issuers,
            "sessions": sessions,
            "exams": exams,
            "credentials": credentials,
            "revocations": revocations,
            "events": copy.deepcopy(self._events),
            "fills": fills,
            "inspections": inspections,
            "shifts": shifts,
        }

    def decision(self, person: str = "OP-7", medium: Optional[str] = None,
                 at: Any = None, knowledge_at: Any = _USE_DEFAULT,
                 **extra: Any) -> oa.AuthorityDecision:
        kwargs: dict[str, Any] = {"person": person, "medium": medium, "at": at}
        if knowledge_at is not _USE_DEFAULT:
            kwargs["knowledge_at"] = knowledge_at
        kwargs.update(extra)
        return oa.evaluate(self.authority_bundle(), **kwargs)

    def can_operate(self, person: str = "OP-7", medium: str = "O2",
                    at: Any = None, **kw: Any) -> bool:
        return self.decision(person, medium=medium, at=at, **kw) == oa.AUTHORIZED

    def authorized_media(self, person: str = "OP-7", at: Any = None, **kw: Any) -> list[str]:
        return self.decision(person, at=at, **kw).authorized_media

    def check_separation(self, **kw: Any) -> dict:
        kw.setdefault("events", self.events)
        kw.setdefault("inspectors", self.inspectors)
        return oa.check_separation(**kw)

    def assert_separated(self, **kw: Any) -> dict:
        result = self.check_separation(**kw)
        if not result["separated"]:
            raise oa.SeparationViolation(result["reason"])
        return result

    def has_inspector_role(self, person: str, at: Any = None) -> bool:
        return oa.inspector_authorized(person, at=at, inspectors=self.inspectors)

    # ------------------------------------------------------------------ #
    # Shifts and late reconciliation
    # ------------------------------------------------------------------ #
    def _completed_shift_ids(self) -> set[str]:
        done = set()
        for event in self._events:
            if event.get("kind") == "fill_completed":
                sid = tc.first_present(event, "shift_id", "shift", default=None)
                if sid:
                    done.add(str(sid))
            if event.get("kind") == "shift_scheduled" and event.get("status") == "completed":
                done.add(str(event.get("id")))
        return done

    def _existing_shift_revocations(self) -> dict[str, dict]:
        found = {}
        for event in self._events:
            if event.get("kind") == "shift_authorization_revoked":
                target = tc.first_present(event, "target", "target_id", "shift_id")
                if target:
                    found[str(target)] = event
        return found

    def shift_view(self, shift_id: str, *, as_of: Any = None) -> dict:
        shift = next((e for e in self._events
                      if e.get("kind") == "shift_scheduled" and str(e.get("id")) == str(shift_id)),
                     None)
        if shift is None:
            raise KeyError(f"unknown shift {shift_id}")
        when = tc.parse_dt(as_of)
        start = tc.first_present(shift, "start", "starts_at", "begin")
        end = tc.first_present(shift, "end", "ends_at", "finish")
        start_dt = tc.parse_dt(start)
        operator = tc.first_present(shift, "operator", "operator_id")
        media = _media_of(shift)
        completed_ids = self._completed_shift_ids()
        executed = (str(shift_id) in completed_ids
                    or shift.get("status") == "completed")
        future = True
        started = True
        end_dt = tc.parse_dt(end)
        if when is not None and end_dt is not None:
            future = end_dt > when
        if when is not None and start_dt is not None:
            started = start_dt <= when
        in_progress = (not executed) and started and future
        unexecuted = (not executed) and future

        # Future shifts are judged at their start under current evidence; a
        # current/in-progress shift is judged "now" so a late revocation removes
        # the operator from the remaining shift rather than waiting for tomorrow.
        if when is not None and start_dt is not None and when > start_dt:
            eval_at: Any = when
            knowledge_at: Any = when
        else:
            eval_at = start
            knowledge_at = start
        statuses = {}
        for medium in media:
            statuses[medium] = self.decision(
                operator, medium=medium, at=eval_at,
                knowledge_at=knowledge_at).status

        prior_revocation = self._existing_shift_revocations().get(str(shift_id))
        all_authorized = all(s == oa.AUTHORIZED for s in statuses.values()) if statuses else False
        if executed:
            current = "completed"
        elif prior_revocation is not None:
            current = "authorization_revoked"
        elif unexecuted and not all_authorized:
            current = "authorization_revoked" if shift.get("initially_authorized") else "blocked"
        elif all_authorized:
            current = "authorized"
        else:
            current = "unauthorized"
        return {"shift_id": str(shift_id), "operator": operator, "media": media,
                "start": start, "end": end, "executed": executed,
                "started": started, "in_progress": in_progress,
                "unexecuted": unexecuted, "evaluated_at": (
                    eval_at.isoformat() if isinstance(eval_at, type(when)) and eval_at else eval_at),
                "medium_status": statuses,
                "all_authorized": all_authorized, "current_status": current,
                "revocation_event": prior_revocation}

    def reconcile_shifts(self, *, as_of: Any = None) -> dict:
        """Withdraw authority for unexecuted shifts invalidated by late evidence.

        Appends one ``shift_authorization_revoked`` event per newly withdrawn
        shift that *was* authorized when scheduled (idempotent).  Unexecuted
        shifts that never had authority are reported as ``blocked`` but are
        not rewritten as revocations.  Completed shifts/fills are never touched.

        Returns ``{"revoked": [stored events], "blocked": [shift ids]}``.
        """
        when = tc.parse_dt(as_of)
        existing = self._existing_shift_revocations()
        newly_revoked: list[dict] = []
        blocked: list[str] = []
        for event in list(self._events):
            if event.get("kind") != "shift_scheduled":
                continue
            sid = str(event["id"])
            if sid in existing:
                continue
            view = self.shift_view(sid, as_of=as_of)
            if not view["unexecuted"]:
                continue
            if view["all_authorized"]:
                continue
            was_authorized = bool(
                event.get("initially_authorized")
                or (isinstance(event.get("authorization_snapshot"), dict)
                    and event["authorization_snapshot"]
                    and all(s == oa.AUTHORIZED
                           for s in event["authorization_snapshot"].values())))
            if not was_authorized:
                blocked.append(sid)
                continue
            triggered_by = self._triggering_evidence(view, when)
            row = self.capture({
                "kind": "shift_authorization_revoked",
                "id": f"SHIFT-REVOKED-{sid}",
                "target": sid,
                "at": as_of,
                "medium_status": view["medium_status"],
                "triggered_by": triggered_by,
                "reason": "迟到证据（签到撤销/考试复核/机构失效）使原已成立的排班授权失效",
            })
            newly_revoked.append(row)
        return {"revoked": newly_revoked, "blocked": blocked}

    def _triggering_evidence(self, view: dict, when: Optional[Any]) -> list[str]:
        """Best-effort ids of late revocation events behind the withdrawal."""
        triggers = []
        for event in self._events:
            if event.get("kind") not in {"session_revocation", "exam_review",
                                         "credential_revocation", "issuer_invalidation"}:
                continue
            triggers.append(str(event.get("id")))
        return triggers

    # ------------------------------------------------------------------ #
    # History / audit
    # ------------------------------------------------------------------ #
    def audit_fill(self, fill_id: str) -> dict:
        fill = next((e for e in self._events
                      if e.get("kind") == "fill_completed" and str(e.get("id")) == str(fill_id)),
                     None)
        if fill is None:
            raise KeyError(f"unknown fill {fill_id}")
        basis = fill.get("basis", {})
        serialized = json.dumps(basis, ensure_ascii=False)
        revoked_basis = []
        for event in self._events:
            if event.get("kind") not in {"session_revocation", "exam_review",
                                         "credential_revocation", "issuer_invalidation"}:
                continue
            target = tc.first_present(event, "target", "target_id")
            if target and str(target) in serialized:
                revoked_basis.append({"target": target, "event_id": event.get("id"),
                                      "kind": event.get("kind"), "at": event.get("at")})
            issuer = event.get("issuer")
            if issuer and event.get("kind") == "issuer_invalidation":
                # An issuer invalidation discredits every credential it issued.
                for cred_event in self._events:
                    if cred_event.get("kind") != "reciprocal_credential":
                        continue
                    if tc.first_present(cred_event, "issuer", "issuer_id") != issuer:
                        continue
                    cid = str(cred_event.get("id"))
                    if cid in serialized:
                        revoked_basis.append({
                            "target": cid, "via_issuer": issuer,
                            "event_id": event.get("id"),
                            "kind": event.get("kind"), "at": event.get("at")})
        return {"fill_id": str(fill_id), "record": copy.deepcopy(fill),
                "basis": copy.deepcopy(basis), "stored_permitted": fill.get("permitted"),
                "later_revoked_basis": revoked_basis,
                "note": "原始充装记录与当时依据原样保留，不被迟到撤销覆盖"}

    def roster(self, at: Any = None) -> dict[str, oa.AuthorityDecision]:
        people = set(self.known_operators)
        for event in self._events:
            for key in ("operator", "person", "operator_id"):
                value = event.get(key)
                if isinstance(value, dict):
                    value = value.get("id")
                if value:
                    people.add(str(value))
            owner = tc.person_of(event)
            if owner and owner != "_":
                people.add(owner)
        return {person: self.decision(person, at=at) for person in sorted(people)}

    def scope_timeline(self, person: str = "OP-7") -> list[dict]:
        """Significant moments and the media scope effective just after them."""
        bundle = self.authority_bundle()
        moments: list[tuple[Any, str, str]] = []
        sessions = [e for e in self._events if e.get("kind") == "training_session"]
        threshold = tc.hours_threshold_reached_at(
            sessions, self.site_rule["minimum_nonoverlap_hours"], person=person,
            strict=False)
        if threshold:
            moments.append((threshold, "training-threshold", "40h 非重叠培训达成"))
        for event in self._events:
            kind = event.get("kind")
            if kind == "practical_exam":
                at = tc.parse_dt(tc.first_present(event, "passed_at", "at", "completed_at"))
                if at:
                    moments.append((at, "practical-exam",
                                    f"{_medium(event.get('medium')) or '设备项'}实操考核"))
            elif kind == "reciprocal_credential":
                start = tc.parse_dt(tc.first_present(event, "valid_from", "issued_at"))
                end = tc.parse_dt(tc.first_present(event, "expires", "expires_at", "valid_to"))
                if start:
                    moments.append((start, "reciprocal-start", f"互认 {event.get('id')} 生效"))
                if end:
                    moments.append((end, "reciprocal-expiry", f"互认 {event.get('id')} 到期"))
            elif kind in {"session_revocation", "exam_review",
                          "credential_revocation", "issuer_invalidation"}:
                at = tc.parse_dt(tc.first_present(event, "at", "effective_at", "recorded_at"))
                if at:
                    moments.append((at, kind, str(event.get("reason") or kind)))
        timeline = []
        seen = set()
        for at, kind, label in sorted(moments, key=lambda x: x[0]):
            key = (at.isoformat(), kind, label)
            if key in seen:
                continue
            seen.add(key)
            decision = self.decision(person, at=at + _one_second())
            timeline.append({"at": at.isoformat(), "kind": kind, "label": label,
                             "authorized_media": decision.authorized_media,
                             "status": decision.status, "media": dict(decision.media)})
        return timeline

    # ------------------------------------------------------------------ #
    # JSON persistence
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "site": self.site, "site_rule": self.site_rule, "media": self.media,
            "operators": self.known_operators,
            "instructors": self.instructors, "courses": self.courses,
            "examiners": self.examiners, "inspectors": self.inspectors,
            "issuers": self.issuers, "events": self.events,
        }

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    def save(self, path: Any) -> Path:
        target = Path(path)
        target.write_text(self.dumps(), encoding="utf-8")
        return target

    @classmethod
    def from_dict(cls, data: dict) -> "QualificationLedger":
        ledger = cls(
            site=data.get("site"), site_rule=data.get("site_rule"),
            media=data.get("media"), operators=data.get("operators"),
            instructors=data.get("instructors"), courses=data.get("courses"),
            examiners=data.get("examiners"), inspectors=data.get("inspectors"),
            issuers=data.get("issuers"))
        for event in data.get("events", []):
            # Preserve seq exactly by appending stored rows directly.
            row = copy.deepcopy(event)
            ledger._seq = max(ledger._seq, int(row.get("seq", 0)))
            ledger._events.append(row)
        return ledger

    @classmethod
    def load(cls, path: Any) -> "QualificationLedger":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def _one_second():
    from datetime import timedelta
    return timedelta(seconds=1)


def _medium(value: Any) -> Optional[str]:
    if value is None:
        return None
    return oa._upper_medium(value)


def _load_json(source: Any) -> dict:
    if isinstance(source, (str, bytes, Path)):
        return json.loads(Path(source).read_text(encoding="utf-8"))
    if hasattr(source, "read"):
        return json.load(source)
    raise TypeError("fixture must be a mapping, path, or readable file")


def _media_of(row: dict) -> list[str]:
    media = oa._medium_list(row.get("media"))
    medium = _medium(row.get("medium"))
    if medium and medium not in media:
        media.append(medium)
    return media or ["O2", "N2"]


# Class aliases for alternate naming conventions.
Ledger = QualificationLedger
AuthorityLedger = QualificationLedger
OperatorLedger = QualificationLedger
QualificationCapture = QualificationLedger
CaptureLedger = QualificationLedger
OperatorQualification = QualificationLedger
QualificationBook = QualificationLedger
AuthorityBook = QualificationLedger


def capture_event(ledger: QualificationLedger, event: Optional[dict] = None, **kw: Any) -> dict:
    """Functional alias: append one event to a ledger."""
    if not isinstance(ledger, QualificationLedger):
        raise TypeError("capture_event expects a QualificationLedger first")
    return ledger.capture(event, **kw)


def ledger_from_fixture(data: Any) -> QualificationLedger:
    """Build a ledger from a fixture dict/path, replaying ``ledger_events``."""
    return QualificationLedger(data)


# Friendly alias.
load_fixture = ledger_from_fixture


def build_ledger(data: Any, **options: Any) -> QualificationLedger:
    """Compose the canonical fixture blocks into a causal-order ledger."""
    if not isinstance(data, dict):
        data = json.loads(Path(data).read_text(encoding="utf-8"))
    return _build_composed(data, **options)


def _build_composed(data: dict, *, route: Optional[str] = None,
                    include_credential: bool = True,
                    include_fills: bool = True,
                    include_shifts: bool = True,
                    include_inspections: bool = True,
                    include_revocations: bool = False,
                    include_route_a: Optional[bool] = None,
                    **_: Any) -> QualificationLedger:
    """Compose the canonical fixture blocks into a ledger in causal order."""
    op = data["operator"]
    ledger = QualificationLedger(
        site=(data.get("site") or {}).get("id", "FILL-STATION-1") if isinstance(data.get("site"), dict) else data.get("site", "FILL-STATION-1"),
        site_rule=data.get("site_rule"),
        instructors=data.get("instructors"), courses=data.get("courses"),
        examiners=data.get("examiners"), inspectors=data.get("inspectors"),
        issuers=data.get("issuers"), operators=[op["id"]])

    for verification in data.get("identity_verifications", []):
        ledger.capture({**verification, "kind": "identity_verification"})

    for session in op.get("sessions", []):
        ledger.add_training_session(session)

    use_route_a = include_route_a if include_route_a is not None else route == "A"
    route_a = data.get("route_a", {})
    if use_route_a:
        for session in route_a.get("additional_sessions", []):
            ledger.add_training_session(session)
        if route_a.get("theory_exam"):
            ledger.record_exam(route_a["theory_exam"])
        for exam in route_a.get("practical_exams", []):
            ledger.record_exam(exam)

    if include_credential and data.get("reciprocal_credential"):
        ledger.add_reciprocal_credential(data["reciprocal_credential"])

    if include_inspections:
        for inspection in data.get("inspections", []):
            ledger.record_inspection(inspection)
    if include_fills:
        for fill in data.get("fills", []):
            ledger.complete_fill(fill)
    if include_shifts:
        for shift in data.get("shifts", []):
            ledger.schedule_shift(shift)
    if include_revocations:
        for rev in data.get("revocations", []):
            kind = str(rev.get("kind") or rev.get("type") or "").lower()
            if "issuer" in kind:
                ledger.invalidate_issuer(rev.get("issuer"), at=rev.get("at"),
                                         reason=rev.get("reason"),
                                         invalidation_id=rev.get("id"))
            elif "exam" in kind:
                ledger.review_exam(rev.get("target"), passed=rev.get("passed", False),
                                   at=rev.get("at"), reason=rev.get("reason"),
                                   review_id=rev.get("id"))
            elif "session" in kind or "signin" in kind or "training" in kind:
                ledger.revoke_training(rev.get("target"), at=rev.get("at"),
                                       reason=rev.get("reason"),
                                       revocation_id=rev.get("id"))
            else:
                ledger.capture(rev)
    return ledger


# Common verb aliases on the ledger for alternate calling conventions.
for _alias, _method in {
    "record_training": QualificationLedger.add_training_session,
    "add_session": QualificationLedger.add_training_session,
    "capture_training": QualificationLedger.add_training_session,
    "record_session": QualificationLedger.add_training_session,
    "add_practical_exam": QualificationLedger.record_exam,
    "capture_exam": QualificationLedger.record_exam,
    "add_exam": QualificationLedger.record_exam,
    "add_external_credential": QualificationLedger.add_reciprocal_credential,
    "record_reciprocal": QualificationLedger.add_reciprocal_credential,
    "grant_reciprocal": QualificationLedger.add_reciprocal_credential,
    "record_shift": QualificationLedger.schedule_shift,
    "add_shift": QualificationLedger.schedule_shift,
    "record_fill": QualificationLedger.complete_fill,
    "add_fill": QualificationLedger.complete_fill,
    "record_inspection_signature": QualificationLedger.record_inspection,
    "authority_at": QualificationLedger.decision,
    "get_decision": QualificationLedger.decision,
    "scope_at": QualificationLedger.authorized_media,
}.items():
    setattr(QualificationLedger, _alias, _method)


def evaluate_qualification(data: Any = None, **kw: Any) -> oa.AuthorityDecision:
    """Functional pass-through to :func:`operator_authority.evaluate`."""
    return oa.evaluate(data, **kw)
