"""Training-period credit with per-person overlap accounting and proof checks.

The seed implementation simply summed ``credited_hours``; two sign-in sheets
covering the same 24 h day therefore booked 48 h.  Hard rule one: for the same
natural person a real minute is credited at most once.  Credit is computed
from proven attendance intervals merged as a time union.

Every segment can be checked segment-by-segment for:

* a qualified instructor covering the session dates,
* a sign-in record,
* a departure/sign-out record,
* a course validity window covering the session,
* no revocation and no claimed hours beyond the real interval.

Late/make-up sessions carry a ``recorded_at`` capture time.  Authority
queries use :func:`hours_available_at` so a session entered after a fill
cannot be back-dated into that fill's basis.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

HOUR = timedelta(hours=1)
MINUTE = timedelta(minutes=1)
# Half-open intervals: two sessions that touch at an endpoint share no minute.
_EPS = timedelta(seconds=1)

__all__ = [
    "parse_dt",
    "first_present",
    "raw_hours",
    "summed_hours",
    "claimed_hours",
    "effective_interval",
    "segment_interval",
    "merge_intervals",
    "union_intervals",
    "union_minutes",
    "union_hours",
    "interval_hours",
    "hours",
    "non_overlapping_hours",
    "nonoverlap_hours",
    "union_credited_hours",
    "valid_sessions",
    "proven_sessions",
    "prove_session",
    "proof_report",
    "hours_by_person",
    "hours_available_at",
    "hours_threshold_reached_at",
    "threshold_reached_at",
    "backfill_conflicts",
    "is_backdated",
]


# --------------------------------------------------------------------------- #
# Time / dictionary helpers
# --------------------------------------------------------------------------- #
def parse_dt(value: Any) -> Optional[datetime]:
    """Parse ISO strings / datetimes into timezone-aware UTC datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        # Station wall-clock times in fixtures are naive; treat them as UTC so
        # every comparison stays on one timeline.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def first_present(mapping: Any, *keys: str, default: Any = None) -> Any:
    """Return the first key whose value is present (including ``False``)."""
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _time_from(value: Any, *keys: str) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = first_present(value, "at", "time", "timestamp", "date", *keys)
        if isinstance(value, dict):  # pragma: no cover - defensive
            value = first_present(value, "at", "time", "timestamp")
    return parse_dt(value)


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _rows(sessions: Any) -> list[dict]:
    if isinstance(sessions, dict):
        sessions = first_present(sessions, "sessions", "training_sessions", "rows", default=[])
    return [s for s in (sessions or []) if isinstance(s, dict)]


def person_of(session: dict) -> Optional[str]:
    value = first_present(session, "person", "person_id", "operator_id",
                          "operator", "worker_id", "employee_id")
    if isinstance(value, dict):
        value = first_present(value, "id", "person_id", "operator_id")
    return None if value is None else str(value)


def _is_revoked(row: dict) -> bool:
    if first_present(row, "revoked", "is_revoked", "cancelled", "canceled",
                     "withdrawn", default=False):
        return True
    status = str(first_present(row, "status", "state", default="")).lower()
    return status in {"revoked", "cancelled", "canceled", "withdrawn", "invalid", "void"}


# --------------------------------------------------------------------------- #
# Naive seed accounting (kept explicitly for regression/demonstration)
# --------------------------------------------------------------------------- #
def raw_hours(sessions: Any) -> float:
    """Seed behaviour: straight sum of booked ``credited_hours``.

    Two overlapping 24 h sign-in sheets return 48.  This is the *claimed*
    total, not the creditable total; tests use it to evidence the inflation.
    """
    total = 0.0
    for row in _rows(sessions):
        value = first_present(row, "credited_hours", "booked_hours",
                              "credit_hours", "hours", default=0)
        try:
            total += float(value or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 6)


# Descriptive aliases.
summed_hours = raw_hours
sum_hours = raw_hours
naive_hours = raw_hours
claimed_hours = raw_hours
cumulative_hours = raw_hours


# --------------------------------------------------------------------------- #
# Interval extraction and union
# --------------------------------------------------------------------------- #
def _attendance_block(row: dict) -> dict:
    block = row.get("attendance")
    return block if isinstance(block, dict) else {}


def effective_interval(session: dict) -> Optional[tuple[datetime, datetime]]:
    """Return the half-open real attendance interval of a segment.

    Prefers actual sign-in/departure timestamps intersected with the scheduled
    ``start``/``end``; falls back to the scheduled interval when no punch
    records exist.  Returns ``None`` for unusable/negative intervals.
    """
    if not isinstance(session, dict) or _is_revoked(session):
        return None

    start = _time_from(first_present(session, "start", "start_at", "started_at",
                                     "from", "begin", "begin_at"))
    end = _time_from(first_present(session, "end", "end_at", "finish",
                                   "finish_at", "to", "until"))
    block = _attendance_block(session)
    sign_in = _time_from(first_present(
        session, "check_in_at", "checked_in_at", "signed_in_at", "sign_in_at",
        "signin_at", "checkin_at", "arrival_at",
        default=first_present(block, "check_in", "sign_in", "signed_in_at", "at")))
    sign_out = _time_from(first_present(
        session, "check_out_at", "checked_out_at", "signed_out_at", "sign_out_at",
        "signout_at", "checkout_at", "departure_at", "departed_at", "leave_at",
        default=first_present(block, "check_out", "sign_out", "signed_out_at",
                              "departure", "leave_at")))

    if start is None and sign_in is not None:
        start = sign_in
    if end is None and sign_out is not None:
        end = sign_out
    if start is None or end is None or end <= start:
        return None

    lo, hi = start, end
    if sign_in is not None:
        lo = max(lo, sign_in)
    if sign_out is not None:
        hi = min(hi, sign_out)
    if hi <= lo:
        return None
    return lo, hi


# Readable alias.
segment_interval = effective_interval


def merge_intervals(intervals: Iterable[tuple[datetime, datetime]]
                    ) -> list[tuple[datetime, datetime]]:
    """Merge overlapping (and endpoint-touching) intervals."""
    ordered = sorted((s, e) for s, e in intervals if s is not None and e is not None and e > s)
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        elif end > merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
    return merged


union_intervals = merge_intervals


def union_minutes(intervals: Iterable[tuple[datetime, datetime]]) -> float:
    return sum((e - s).total_seconds() for s, e in merge_intervals(intervals)) / 60.0


def union_hours(intervals: Iterable[tuple[datetime, datetime]]) -> float:
    return round(union_minutes(intervals) / 60.0, 6)


def interval_hours(session: dict) -> float:
    interval = effective_interval(session)
    return 0.0 if interval is None else round((interval[1] - interval[0]).total_seconds() / 3600.0, 6)


def _eligible_intervals(sessions: Any, person: Optional[str] = None
                        ) -> list[tuple[datetime, datetime]]:
    intervals = []
    for row in _rows(sessions):
        if person is not None:
            owner = person_of(row)
            if owner is not None and owner != str(person):
                continue
        interval = effective_interval(row)
        if interval is not None:
            intervals.append(interval)
    return intervals


def hours(sessions: Any = None, *, person: Optional[str] = None,
          strict: bool = False, instructors: Optional[dict] = None,
          courses: Optional[dict] = None) -> float:
    """Creditable non-overlapping training hours.

    Default is the time-union over usable, non-revoked intervals.  With
    ``strict=True`` only segment-by-segment proven rows count.  Accepts a
    session list or an operator/fixture mapping containing ``sessions``.
    """
    if sessions is None:
        return 0.0
    if strict:
        rows = valid_sessions(sessions, instructors=instructors, courses=courses,
                              person=person)
    else:
        rows = _rows(sessions)
    return union_hours(_eligible_intervals(rows, person=None if strict else person))


# Intent-revealing aliases for the corrected metric.
non_overlapping_hours = hours
nonoverlap_hours = hours
non_overlap_hours = hours
deduplicated_hours = hours
dedup_hours = hours
effective_hours = hours
real_hours = hours
clock_hours = hours
union_credited_hours = hours


def hours_by_person(sessions: Any, **proof_kwargs: Any) -> dict[str, float]:
    """Union hours grouped by natural person."""
    groups: dict[str, list[dict]] = {}
    for row in _rows(sessions):
        groups.setdefault(person_of(row) or "_", []).append(row)
    return {person: hours(rows, **proof_kwargs) for person, rows in sorted(groups.items())}


def hours_available_at(sessions: Any, at: Any, *, person: Optional[str] = None,
                       knowledge_at: Any = None, strict: bool = False,
                       instructors: Optional[dict] = None,
                       courses: Optional[dict] = None) -> float:
    """Union hours completed and already captured by instant ``at``.

    Intervals are clipped at ``at`` (a session still running has not yet
    earned its future minutes).  When ``knowledge_at`` is given, rows carrying
    a later ``recorded_at``/``captured_at`` are ignored -- this is what stops
    a make-up sheet entered after a fill from being back-dated onto it.
    """
    when = parse_dt(at)
    known = parse_dt(knowledge_at) if knowledge_at is not None else when
    rows = _rows(sessions)
    if strict:
        rows = valid_sessions(rows, instructors=instructors, courses=courses, person=person)
    intervals = []
    for row in rows:
        if person is not None:
            owner = person_of(row)
            if owner is not None and owner != str(person):
                continue
        recorded = parse_dt(first_present(row, "recorded_at", "captured_at",
                                          "created_at", "entered_at"))
        if known is not None and recorded is not None and recorded > known + _EPS:
            continue
        interval = effective_interval(row)
        if interval is None or when is None:
            continue
        start, end = interval
        if start >= when:
            continue
        intervals.append((start, min(end, when)))
    return union_hours(intervals)


def hours_threshold_reached_at(sessions: Any, required_hours: float, *,
                               person: Optional[str] = None,
                               strict: bool = False,
                               instructors: Optional[dict] = None,
                               courses: Optional[dict] = None
                               ) -> Optional[datetime]:
    """Earliest instant at which the union total reaches ``required_hours``."""
    if strict:
        rows = valid_sessions(sessions, instructors=instructors, courses=courses,
                              person=person)
        intervals = _eligible_intervals(rows)
    else:
        intervals = _eligible_intervals(sessions, person)
    merged = merge_intervals(intervals)
    needed = timedelta(hours=float(required_hours))
    accrued = timedelta(0)
    for start, end in merged:
        length = end - start
        if accrued + length + _EPS >= needed:
            return start + (needed - accrued)
        accrued += length
    return None


threshold_reached_at = hours_threshold_reached_at


# --------------------------------------------------------------------------- #
# Segment proof checks (instructor / sign-in / departure / course validity)
# --------------------------------------------------------------------------- #
def _resolve_named(ref: Any, registry: Optional[dict]) -> tuple[Optional[str], Optional[dict]]:
    if isinstance(ref, dict):
        rid = str(first_present(ref, "id", "instructor_id", "code", default="")) or None
        return rid, ref
    if ref is None:
        return None, None
    rid = str(ref)
    rec = None
    if isinstance(registry, dict):
        rec = registry.get(rid) or registry.get(ref)
        if isinstance(rec, dict):
            return rid, rec
    return rid, None


def _window_covers(record: dict, start: datetime, end: datetime) -> tuple[bool, str]:
    valid_from = parse_dt(first_present(record, "valid_from", "valid_start",
                                        "issued_at", "issue_date", "from"))
    valid_to = parse_dt(first_present(record, "valid_to", "valid_until",
                                      "expires", "expires_at", "expiry", "to",
                                      "valid_through"))
    if valid_from and valid_from > start + _EPS:
        return False, "资格/课程尚未生效"
    if valid_to and valid_to < end - _EPS:
        return False, "资格/课程在授课时段前失效"
    return True, ""


def _qualified(record: Optional[dict]) -> bool:
    if not isinstance(record, dict):
        return False
    explicit = first_present(record, "qualified", "is_qualified", "certified",
                             "authorized", default=None)
    if explicit is not None:
        return bool(explicit)
    if first_present(record, "revoked", "suspended", default=False):
        return False
    return bool(first_present(record, "certificate", "credential", "license",
                              "certification", "cert_no", default=None))


def prove_session(session: dict, *, instructors: Optional[dict] = None,
                 courses: Optional[dict] = None) -> dict:
    """Validate one segment; return ``{valid, reasons, evidence, interval}``."""
    reasons: list[str] = []
    evidence: list[str] = []
    sid = str(first_present(session, "id", "session_id", default="?"))

    if not isinstance(session, dict):
        return {"id": sid, "valid": False, "reasons": ["记录不是对象"],
                "evidence": evidence, "interval": None, "hours": 0.0}

    if _is_revoked(session):
        reasons.append("签到/课程已撤销")
        evidence.append(f"{sid}:revoked")

    interval = effective_interval(session)
    if interval is None:
        reasons.append("培训时段缺失、倒置或无有效签到离场区间")
        return {"id": sid, "valid": False, "reasons": reasons,
                "evidence": evidence, "interval": None, "hours": 0.0}
    start, end = interval
    evidence.append(f"{sid}:interval[{start.isoformat()}..{end.isoformat()})")

    # Claimed booked hours must not exceed the real minutes in the room.
    claimed = first_present(session, "credited_hours", "booked_hours",
                            "credit_hours", "hours")
    if claimed is not None:
        try:
            if float(claimed) > interval_hours(session) + 1e-6:
                reasons.append("记账时数超过真实时段长度")
        except (TypeError, ValueError):
            reasons.append("记账时数无法解析")

    block = _attendance_block(session)
    sign_in_raw = first_present(
        session, "check_in_at", "checked_in_at", "signed_in_at", "sign_in_at",
        "signin", "checkin", default=first_present(block, "check_in", "sign_in"))
    sign_out_raw = first_present(
        session, "check_out_at", "checked_out_at", "signed_out_at", "sign_out_at",
        "signout", "checkout", "departure_at", "departed_at",
        default=first_present(block, "check_out", "sign_out", "departure"))
    if sign_in_raw in (None, "", {}):
        reasons.append("缺少签到记录")
    else:
        evidence.append(f"{sid}:sign-in")
    if sign_out_raw in (None, "", {}):
        reasons.append("缺少离场/签退记录")
    else:
        evidence.append(f"{sid}:departure")

    # Instructor qualification.
    ins_ref = first_present(session, "instructor", "instructor_id", "trainer",
                            "trainer_id")
    ins_id, ins_rec = _resolve_named(ins_ref, instructors)
    if ins_ref is None:
        reasons.append("缺少授课者")
    elif ins_rec is None and not isinstance(ins_ref, dict):
        # Referenced but no registry: cannot prove qualification.
        reasons.append(f"授课者 {ins_id} 资格无可验证记录")
    elif not _qualified(ins_rec if ins_rec is not None else (ins_ref if isinstance(ins_ref, dict) else None)):
        reasons.append(f"授课者 {ins_id or ''} 未具备有效资格".strip())
    else:
        ok, why = _window_covers(ins_rec if ins_rec is not None else ins_ref, start, end)
        if not ok:
            reasons.append("授课者资格" + why)
        else:
            evidence.append(f"instructor:{ins_id or 'inline'}")

    # Course validity.
    course_ref = first_present(session, "course", "course_id", "program",
                               "program_id", "curriculum")
    course_id, course_rec = _resolve_named(course_ref, courses)
    if course_ref is None:
        reasons.append("缺少课程及有效期")
    elif course_rec is None and not isinstance(course_ref, dict):
        reasons.append(f"课程 {course_id} 有效期无可验证记录")
    else:
        course = course_rec if course_rec is not None else course_ref
        course_dead = bool(first_present(course, "revoked", "cancelled",
                                         "canceled", "suspended", default=False))
        if course_dead or course.get("active") is False or course.get("valid") is False:
            reasons.append("课程已停开/作废")
        else:
            ok, why = _window_covers(course, start, end)
            if not ok:
                reasons.append("课程有效期" + why)
            else:
                evidence.append(f"course:{course_id or 'inline'}")

    return {
        "id": sid,
        "valid": not reasons,
        "reasons": reasons,
        "evidence": evidence,
        "interval": (start, end),
        "hours": interval_hours(session),
    }


def proof_report(sessions: Any, *, instructors: Optional[dict] = None,
                 courses: Optional[dict] = None) -> list[dict]:
    return [prove_session(row, instructors=instructors, courses=courses)
            for row in _rows(sessions)]


def valid_sessions(sessions: Any, *, instructors: Optional[dict] = None,
                   courses: Optional[dict] = None,
                   person: Optional[str] = None) -> list[dict]:
    """Rows whose every segment proof checks out."""
    rows = _rows(sessions)
    result = []
    for row in rows:
        if person is not None:
            owner = person_of(row)
            if owner is not None and owner != str(person):
                continue
        if prove_session(row, instructors=instructors, courses=courses)["valid"]:
            result.append(row)
    return result


proven_sessions = valid_sessions


# --------------------------------------------------------------------------- #
# Back-dating detection
# --------------------------------------------------------------------------- #
def is_backdated(session: dict, fills: Iterable[dict]) -> bool:
    """True when a session captured after a fill claims to end before that fill.

    Such a make-up row must not be used to retroactively authorize the fill.
    """
    recorded = parse_dt(first_present(session, "recorded_at", "captured_at",
                                      "created_at", "entered_at"))
    interval = effective_interval(session)
    if recorded is None or interval is None:
        return False
    end = interval[1]
    for fill in fills or []:
        when = parse_dt(first_present(fill, "filled_at", "at", "completed_at",
                                      "timestamp", "time"))
        if when is None:
            continue
        if recorded > when + _EPS and end <= when + _EPS:
            return True
    return False


def backfill_conflicts(sessions: Iterable[dict], fills: Iterable[dict]) -> list[dict]:
    fills = list(fills or [])
    conflicts = []
    for session in sessions or []:
        if is_backdated(session, fills):
            conflicts.append({
                "session_id": first_present(session, "id", "session_id"),
                "recorded_at": first_present(session, "recorded_at", "captured_at"),
                "reason": "补课记录晚于作业录入，却声称时段早于已完成作业；不得倒填授权",
            })
    return conflicts


# Additional naming aliases for callers using task-domain vocabulary.
calculate_hours = hours
calculate_credit = hours
total_hours = hours
credit_total = hours
deduplicate_intervals = merge_intervals
validate_session = prove_session
session_proof = prove_session
non_overlap_minutes = union_minutes
valid_hours = hours
creditable_hours = hours
counted_hours = hours
proven_hours = hours
