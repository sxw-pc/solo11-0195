"""Recomputable ventilation safety boundary over an append-only event log.

The module deliberately keeps no mutable authoritative state: every event
(sensor samples, late calibration updates, damper commands and receipts,
fresh-air flow, muster sign-ins, purge and retest evidence, plan selections)
is appended with its real timestamp, and :meth:`VentilationCapture.snapshot`
rebuilds the whole timeline from scratch. Late-arriving evidence therefore
cannot rewrite what physically happened; it can only add new entries — in
particular, a revoked return-to-work commitment keeps the earlier stop,
evacuation and lost-production history intact.

Two safe dispositions are evaluated against the same inputs:

* Plan A — both bays stop and evacuate; each bay returns only after the shared
  duct is fully purged, its own repeat sampling is safe and release is
  authorized. No production in either bay meanwhile.
* Plan B — both bays still stop and evacuate first; then a non-source bay may
  resume early only after a damper receipt proves physical isolation from the
  shared return duct, independent fresh air continuously reaches at least
  :data:`FRESH_AIR_MIN_FLOW` m³/min, and repeat safe sampling plus personnel
  muster are complete. Missing receipt forces fallback to the Plan A lockdown.
"""
from .air_monitor import ALARM, WARNING, FAULT, NORMAL, grade_reading

FRESH_AIR_MIN_FLOW = 350      # m3/min independent fresh air required for plan B
REQUIRED_SAFE_SAMPLES = 2     # distinct repeat safe samples before any restart
# Opt-in time window for callers that want time-based staleness; the engine
# itself only treats an explicit stale/missing status as a fault, so sparse
# legitimate sampling is never silently reinterpreted as zero evidence.
STALE_AFTER_MINUTES = 5

# Stable blocker codes surfaced in plan eligibility; tests pin these names.
B_MUSTER_INCOMPLETE = 'muster_incomplete'
B_RETEST_MISSING = 'retest_missing'
A_PURGE_MISSING = 'duct_not_purged'
A_AUTHORIZATION_MISSING = 'release_not_authorized'
B_SOURCE_BAY_LOCKED = 'source_bay_locked'
B_DAMPER_NOT_PROVEN = 'damper_receipt_missing'
B_FRESH_AIR_UNAVAILABLE = 'fresh_air_unavailable'
B_FLOW_BELOW_MINIMUM = 'fresh_air_below_350'
B_SAMPLES_INSUFFICIENT = 'repeat_safe_samples_insufficient'
B_LOCAL_SENSOR_UNSAFE = 'local_sensor_unsafe'
B_SHARED_DUCT_PROPAGATION = 'shared_return_duct_propagation'


class VentilationCapture:
    """Append-only safety timeline for filling bays sharing return ducts."""

    def __init__(self, bays, dampers=(), assembly_point='M1'):
        self.bays = [dict(b) for b in bays]
        self.dampers = [dict(d) for d in dampers]
        self.assembly_point = assembly_point
        self.events = []
        self._seq = 0

    @classmethod
    def from_fixture(cls, data):
        """Seed a capture from the static ventilation-shutdown fixture."""
        cap = cls(data['bays'], data.get('dampers', ()),
                  data.get('assembly_point', 'M1'))
        first_minute = data['readings'][0]['at_minute'] if data['readings'] else 0
        for reading in data['readings']:
            cap.append({'kind': 'sensor_reading', **reading})
        for damper in data.get('dampers', ()):
            if damper.get('state') == 'closed':
                cap.append({'kind': 'damper_command',
                            'id': f'SEED-CMD-{damper["id"]}',
                            'damper_id': damper['id'],
                            'bay': damper['bay'], 'return_duct': damper['return_duct'],
                            'command': 'close', 'at_minute': first_minute})
                if damper.get('receipt') == 'in_position':
                    cap.append({'kind': 'damper_receipt',
                                'id': f'SEED-RCPT-{damper["id"]}',
                                'damper_id': damper['id'],
                                'receipt': 'in_position', 'at_minute': first_minute})
        fresh = data.get('independent_fresh_air')
        if fresh and fresh.get('available'):
            cap.append({'kind': 'fresh_air', 'id': f'SEED-FLOW-{fresh["bay"]}',
                        'bay': fresh['bay'],
                        'flow_m3_per_minute': fresh['flow_m3_per_minute'],
                        'at_minute': first_minute})
        return cap

    def append(self, event):
        """Append one evidence event; identity and timestamp are required."""
        if not event.get('id'):
            raise ValueError('event requires an id')
        if event.get('at_minute') is None:
            raise ValueError('event requires at_minute evidence')
        if any(e['id'] == event['id'] for e in self.events):
            raise ValueError(f"duplicate event id {event['id']}")
        self._seq += 1
        stored = dict(event)
        stored['_seq'] = self._seq
        self.events.append(stored)
        return stored

    def snapshot(self):
        """Rebuild the complete safety boundary from the event log."""
        return _replay(self.bays, self.dampers, self.assembly_point, self.events)

    def try_resume(self, bay, plan, at_minute):
        """Request a restart under ``plan``; gates are re-checked on replay.

        The gate decision is made against the evidence *before* appending the
        request, then the request itself is recorded (accepted or rejected), so
        rebuilding the log reproduces the full command history — including
        ``run_rejected`` entries — without any mutable decision state.
        """
        eligible = self.snapshot()['bays'][bay]['plan_eligible'][plan]
        self.append({'kind': 'resume_command',
                     'id': f'RESUME-{bay}-{plan}-{at_minute}',
                     'at_minute': at_minute, 'bay': bay, 'plan': plan})
        return eligible


def _ordered(events):
    return sorted(events, key=lambda e: (e['at_minute'], e['_seq']))


def _replay(bays, dampers, assembly_point, events):
    engine = _Engine(bays, dampers, assembly_point)
    ordered = _ordered(events)
    engine.events = ordered
    # A sensor that reports later in the ordered log is not "missing" while an
    # earlier same-minute event is being replayed; only a sensor absent from
    # the complete evidence stream can be judged missing.
    engine.sensors_ever_seen = {
        e.get('sensor_id', e['id']) for e in ordered if e['kind'] == 'sensor_reading'
    }
    for event in ordered:
        engine.ingest(event)
    engine.evaluate(at_minute=engine.last_minute)
    return engine.output()


class _Engine:
    def __init__(self, bays, dampers, assembly_point):
        self.bays = [dict(b) for b in bays]
        self.bay_ids = [b['id'] for b in self.bays]
        self.dampers = [dict(d) for d in dampers]
        self.assembly_point = assembly_point
        self.events = []
        self.sensors_ever_seen = set()
        self.last_minute = 0

        # Sensor / calibration evidence. New samples replace older samples of
        # the same logical sensor; the full history remains in self.events.
        self.latest_reading = {}       # logical sensor_id -> latest reading
        self.calibration = {}          # sensor_id -> calibration error (latest version)
        self.sensor_bay = {}           # sensor_id -> bay
        # Damper physical state
        self.damper_receipt = {}       # damper_id -> 'in_position' | 'fault' | None
        self.damper_command = {}       # damper_id -> last command
        # Fresh air flow history per bay
        self.flow_events = {b: [] for b in self.bay_ids}
        # Personnel
        self.musters = []              # list of muster events
        # Duct / bay evidence
        self.purged_ducts = set()
        self.retests = {b: [] for b in self.bay_ids}
        self.authorized = {b: False for b in self.bay_ids}
        # Derived timeline
        self.ledger = []               # equipment command entries
        self.evacuations = []          # evacuation sequence entries
        self.running = {b: True for b in self.bay_ids}
        self.stop_reason = {b: None for b in self.bay_ids}
        self.stop_start = {b: None for b in self.bay_ids}
        self.last_stop_minute = {b: None for b in self.bay_ids}
        self.resume_plan = {b: None for b in self.bay_ids}
        self.intervals = {b: [] for b in self.bay_ids}
        self.alarm_sources = set()
        self.evac_logged = set()
        self.selected_plan = None

    # ------------------------------------------------------------------ ingest
    def ingest(self, event):
        kind = event['kind']
        self.last_minute = event['at_minute']
        if kind == 'sensor_reading':
            logical = event.get('sensor_id', event['id'])
            self.latest_reading[logical] = event
            self.sensor_bay[logical] = event['bay']
            if logical not in self.calibration:
                self.calibration[logical] = event.get('calibration_error_ppm', 0) or 0
        elif kind == 'calibration_update':
            self.calibration[event['sensor_id']] = event['calibration_error_ppm']
        elif kind == 'damper_command':
            self.damper_command[event['damper_id']] = event['command']
        elif kind == 'damper_receipt':
            self.damper_receipt[event['damper_id']] = event['receipt']
        elif kind == 'fresh_air':
            self.flow_events[event['bay']].append(event)
        elif kind == 'muster':
            self.musters.append(event)
        elif kind == 'duct_purged':
            self.purged_ducts.add(event['duct'])
        elif kind == 'bay_retest':
            self.retests[event['bay']].append(event)
        elif kind == 'release_authorization':
            self.authorized[event['bay']] = bool(event.get('authorized', True))
        elif kind == 'plan_selected':
            self.selected_plan = event['plan']
        elif kind == 'resume_command':
            self._resume_request(event)
        else:
            raise ValueError(f'unknown event kind {kind}')
        self.evaluate(event['at_minute'])

    # ------------------------------------------------------------- connectivity
    def _isolated_bays(self):
        """Bays whose own damper is physically proven closed in position."""
        isolated = set()
        for damper in self.dampers:
            if self.damper_receipt.get(damper['id']) == 'in_position':
                isolated.add(damper['bay'])
        return isolated

    def _connected_pairs(self):
        isolated = self._isolated_bays()
        pairs = set()
        ducts = {}
        for bay in self.bays:
            if bay['id'] not in isolated:
                ducts.setdefault(bay['return_duct'], set()).add(bay['id'])
        for members in ducts.values():
            members = sorted(members)
            for i, left in enumerate(members):
                for right in members[i + 1:]:
                    pairs.add(frozenset((left, right)))
        return pairs

    def _missing_sensor_faults(self):
        """Bays whose declared fixed sensor is absent from the whole evidence.

        Missing is not zero, but a sensor that reports later in the ordered
        stream must not be judged missing while an earlier same-minute event is
        being replayed. Staleness of a sensor already seen is handled by the
        reading grade itself; only sensors never seen anywhere count missing.
        """
        if not self.latest_reading:
            return set()
        faults = set()
        for bay in self.bays:
            declared = bay.get('sensors')
            known = {s for s, b in self.sensor_bay.items() if b == bay['id']}
            expected = set(declared) if declared else known
            if expected and expected - self.sensors_ever_seen:
                faults.add(bay['id'])
        return faults

    def _grades(self):
        """Worst-case grade per bay from current sensor evidence.

        Both raw sensor readings and dedicated repeat retests count as fresh
        samples: a safe retest keeps an isolated bay provably normal even when
        its original reading has aged. Declared sensors that never produced a
        sample force :data:`FAULT` — missing is never zero.
        """
        samples = {b: [] for b in self.bay_ids}
        for sensor_id, event in self.latest_reading.items():
            reading = dict(event)
            reading['calibration_error_ppm'] = self.calibration.get(
                sensor_id, event.get('calibration_error_ppm', 0) or 0)
            samples[event['bay']].append(reading)
        for bay, retests in self.retests.items():
            for event in retests:
                samples[bay].append(dict(event))
        grades = {b: set() for b in self.bay_ids}
        for bay, items in samples.items():
            for reading in items:
                grades[bay].add(grade_reading(reading))
        for bay in self._missing_sensor_faults():
            grades[bay].add(FAULT)
        priority = {FAULT: 3, ALARM: 2, WARNING: 1, NORMAL: 0}
        return {b: (max(gs, key=lambda g: priority[g]) if gs else NORMAL)
                for b, gs in grades.items()}

    def _fixed_sensor_grades(self):
        """Grades derived only from fixed sensors, never from retest samples.

        Source-bay identity is a property of the fixed monitoring sensors:
        safe repeat sampling cannot relabel a bay as the leak source, and a
        stale/missing fixed sensor keeps its bay unsafe rather than reading 0.
        """
        grades = {b: set() for b in self.bay_ids}
        for sensor_id, event in self.latest_reading.items():
            reading = dict(event)
            reading['calibration_error_ppm'] = self.calibration.get(
                sensor_id, event.get('calibration_error_ppm', 0) or 0)
            grades[event['bay']].add(grade_reading(reading))
        for bay in self._missing_sensor_faults():
            grades[bay].add(FAULT)
        priority = {FAULT: 3, ALARM: 2, WARNING: 1, NORMAL: 0}
        return {b: (max(gs, key=lambda g: priority[g]) if gs else NORMAL)
                for b, gs in grades.items()}

    # --------------------------------------------------------------- evaluation
    def evaluate(self, at_minute):
        grades = self._grades()
        fixed = self._fixed_sensor_grades()
        # The leak-source identity sticks for the whole incident, but only a
        # point-level ALARM or a FAULT proves a bay as the source. A WARNING
        # (calibration band crosses the threshold while the point stays under)
        # is uncertainty, not proof that this bay is where gas is escaping.
        for bay, grade in fixed.items():
            if grade in (ALARM, FAULT):
                self.alarm_sources.add(bay)
        pairs = self._connected_pairs()
        unsafe = {b for b, g in grades.items() if g != NORMAL}
        propagated = set(unsafe)
        changed = True
        while changed:
            changed = False
            for pair in pairs:
                if pair & propagated and not pair <= propagated:
                    propagated |= pair
                    changed = True

        # 1. Stop/evacuation boundary: connected propagation stops every bay.
        # A bay that has previously held a restart commitment is revoked with
        # its blocker evidence; only first-time stops use the plain stop path.
        for bay in self.bay_ids:
            if bay in propagated and self.running[bay]:
                if self.resume_plan[bay]:
                    if grades[bay] != NORMAL:
                        blockers = [B_LOCAL_SENSOR_UNSAFE, f'sensor_grade_{grades[bay]}']
                    elif bay not in unsafe:
                        blockers = [B_SHARED_DUCT_PROPAGATION]
                    else:
                        _, gate_blockers = self.plan_gates(bay)
                        blockers = gate_blockers.get(self.resume_plan[bay], ['evidence_lost'])
                    self._revoke(bay, at_minute, blockers)
                else:
                    self._stop(bay, at_minute, grades, propagated, unsafe)

        # 2. Re-evaluate every running bay: late evidence may revoke a restart.
        # Only the plan this bay actually restarted under can be revoked.
        for bay in self.bay_ids:
            if self.running[bay] and self.resume_plan[bay]:
                if grades[bay] != NORMAL:
                    self._revoke(bay, at_minute,
                                 [B_LOCAL_SENSOR_UNSAFE, f'sensor_grade_{grades[bay]}'])
                    continue
                eligible, blockers = self.plan_gates(bay)
                active = self.resume_plan[bay]
                if not eligible[active]:
                    self._revoke(bay, at_minute, blockers[active])

    def _stop(self, bay, at_minute, grades, propagated, unsafe):
        own_grade = grades.get(bay, NORMAL)
        if own_grade != NORMAL:
            reason = f'local_sensor_{own_grade}'
        else:
            reason = 'shared_return_duct_propagation'
        self.running[bay] = False
        self.stop_reason[bay] = reason
        self.stop_start[bay] = at_minute
        self.last_stop_minute[bay] = at_minute
        self.resume_plan[bay] = None
        self.ledger.append({'id': f'CMD-{len(self.ledger) + 1:03d}', 'bay': bay,
                            'command': 'stop', 'at_minute': at_minute,
                            'reason': reason, 'evidence': self._stop_evidence(bay)})
        if bay not in self.evac_logged:
            self.evac_logged.add(bay)
            self.evacuations.append(self._evacuation_entry(bay, at_minute))

    def _staff(self, bay):
        return next(b for b in self.bays if b['id'] == bay)['staff']

    def _evacuation_entry(self, bay, at_minute):
        signed, missing = self._muster(bay)
        return {'id': f'EVAC-{len(self.evacuations) + 1:03d}',
                'bay': bay, 'at_minute': at_minute,
                'assembly_point': self.assembly_point,
                'staff': list(self._staff(bay)),
                'signed_in': signed, 'missing': missing}

    def _stop_evidence(self, bay):
        evidence = []
        for sensor_id, event in self.latest_reading.items():
            if event['bay'] == bay:
                evidence.append(event['id'])
        duct = next(b['return_duct'] for b in self.bays if b['id'] == bay)
        evidence.append(f'duct:{duct}')
        return evidence

    def _revoke(self, bay, at_minute, blockers):
        self.running[bay] = False
        self.stop_reason[bay] = 'return_to_work_revoked:' + ','.join(blockers)
        self.stop_start[bay] = at_minute
        self.last_stop_minute[bay] = at_minute
        self.ledger.append({'id': f'CMD-{len(self.ledger) + 1:03d}', 'bay': bay,
                            'command': 'stop', 'at_minute': at_minute,
                            'reason': 'late_evidence_revokes_restart',
                            'revoked_plan': self.resume_plan[bay],
                            'blockers': blockers,
                            'evidence': [e['id'] for e in self.events
                                         if e['at_minute'] == at_minute]})
        self.evac_logged.add(bay)
        self.evacuations.append(self._evacuation_entry(bay, at_minute))

    def _resume_request(self, event):
        """Apply an appended restart request if its plan gates still pass."""
        bay, plan, at_minute = event['bay'], event['plan'], event['at_minute']
        eligible, blockers = self.plan_gates(bay)
        if self.running[bay] or not eligible[plan]:
            self.ledger.append({'id': f'CMD-{len(self.ledger) + 1:03d}', 'bay': bay,
                                'command': 'run_rejected', 'at_minute': at_minute,
                                'plan': plan, 'blockers': blockers[plan],
                                'evidence': [event['id']]})
            return
        self.running[bay] = True
        self.resume_plan[bay] = plan
        self.intervals[bay].append({'stop_minute': self.stop_start[bay],
                                    'resume_minute': at_minute, 'plan': plan})
        self.stop_start[bay] = None
        self.evac_logged.discard(bay)
        self.ledger.append({'id': f'CMD-{len(self.ledger) + 1:03d}', 'bay': bay,
                            'command': 'run', 'at_minute': at_minute,
                            'plan': plan, 'evidence': self._resume_evidence(bay, plan)})

    # -------------------------------------------------------------- restart gates
    def plan_gates(self, bay):
        """Eligibility of plans A and B with explicit blocker codes.

        All evidence must be newer than the bay's latest stop: after a revoked
        restart the old muster, sampling and flow evidence cannot be replayed
        as proof for a new commitment.
        """
        cutoff = self.last_stop_minute[bay]
        blockers = {'A': [], 'B': []}
        signed, missing = self._muster(bay, cutoff)
        if missing:
            blockers['A'].append(B_MUSTER_INCOMPLETE)
            blockers['B'].append(B_MUSTER_INCOMPLETE)
        safe_retests = [e for e in self.retests[bay]
                        if (cutoff is None or e['at_minute'] > cutoff)
                        and e.get('result', 'safe') == 'safe']
        if len(safe_retests) < REQUIRED_SAFE_SAMPLES:
            blockers['A'].append(B_RETEST_MISSING)
            blockers['B'].append(B_RETEST_MISSING)
        duct = next(b['return_duct'] for b in self.bays if b['id'] == bay)

        # Plan A: full duct purge plus explicit release authorization.
        if duct not in self.purged_ducts:
            blockers['A'].append(A_PURGE_MISSING)
        if not self.authorized[bay]:
            blockers['A'].append(A_AUTHORIZATION_MISSING)

        # Plan B: only non-source bays, physical isolation, independent air.
        if bay in self.alarm_sources:
            blockers['B'].append(B_SOURCE_BAY_LOCKED)
        if bay not in self._isolated_bays():
            blockers['B'].append(B_DAMPER_NOT_PROVEN)
        flows = [e for e in self.flow_events.get(bay, [])
                 if cutoff is None or e['at_minute'] > cutoff]
        if not flows:
            blockers['B'].append(B_FRESH_AIR_UNAVAILABLE)
        else:
            if flows[-1]['flow_m3_per_minute'] < FRESH_AIR_MIN_FLOW:
                blockers['B'].append(B_FLOW_BELOW_MINIMUM)
            elif any(e['flow_m3_per_minute'] < FRESH_AIR_MIN_FLOW for e in flows):
                blockers['B'].append(B_FLOW_BELOW_MINIMUM)
        eligible_safe = [e for e in safe_retests
                         if e['gas_ppm'] + e.get('calibration_error_ppm', 0) < e['alarm_ppm']]
        if len(eligible_safe) < REQUIRED_SAFE_SAMPLES:
            blockers['B'].append(B_SAMPLES_INSUFFICIENT)
        return ({'A': not blockers['A'], 'B': not blockers['B']}, blockers)

    def _muster(self, bay, cutoff=None):
        staff = self._staff(bay)
        signed = {e['employee'] for e in self.musters
                  if e['employee'] in staff and e.get('assembly_point') == self.assembly_point
                  and e.get('signature')
                  and (cutoff is None or e['at_minute'] > cutoff)}
        return sorted(signed), [s for s in staff if s not in signed]

    def _resume_evidence(self, bay, plan):
        evidence = [e['id'] for e in self.retests[bay][-REQUIRED_SAFE_SAMPLES:]]
        evidence += [e['id'] for e in self.musters
                     if e['employee'] in next(b for b in self.bays if b['id'] == bay)['staff']]
        if plan == 'A':
            duct = next(b['return_duct'] for b in self.bays if b['id'] == bay)
            evidence.append(f'purge:{duct}')
            evidence.append(f'authorization:{bay}')
        else:
            for damper in self.dampers:
                if damper['bay'] == bay and self.damper_receipt.get(damper['id']) == 'in_position':
                    evidence.append(f'damper_receipt:{damper["id"]}')
            cutoff = self.last_stop_minute[bay]
            evidence += [e['id'] for e in self.flow_events[bay]
                         if cutoff is None or e['at_minute'] > cutoff]
        return evidence

    # ------------------------------------------------------------------- output
    def output(self):
        at_minute = self.last_minute
        grades = self._grades()
        eligible, blockers = {}, {}
        for bay in self.bay_ids:
            eligible[bay], blockers[bay] = self.plan_gates(bay)
        downtime = self._downtime()
        bay_states = {}
        for bay in self.bay_ids:
            signed, missing = self._muster(bay, self.last_stop_minute[bay])
            bay_states[bay] = {
                'equipment': 'run' if self.running[bay] else 'stop',
                'alarm_grade': grades[bay],
                'alarm_source': bay in self.alarm_sources,
                'physically_isolated': bay in self._isolated_bays(),
                'evacuation': ('complete' if not missing else 'required')
                if not self.running[bay] or self.evacuations else 'not_required',
                'signed_in': signed,
                'missing_staff': missing,
                'resume_plan': self.resume_plan[bay],
                'plan_eligible': eligible[bay],
                'blockers': blockers[bay],
                'lost_production_minutes': downtime[bay]['lost_minutes'],
            }
        return {
            'at_minute': at_minute,
            'selected_plan': self.selected_plan,
            'bays': bay_states,
            'connected_pairs': sorted(map(sorted, self._connected_pairs())),
            'isolated_bays': sorted(self._isolated_bays()),
            'command_ledger': list(self.ledger),
            'evacuation_ledger': list(self.evacuations),
            'downtime_intervals': {b: list(v) for b, v in self.intervals.items()},
            'lost_production': downtime,
            'events': [dict(e, _seq=e['_seq']) for e in _ordered(self.events)],
        }

    def _downtime(self):
        result = {}
        for bay in self.bay_ids:
            intervals = [dict(item) for item in self.intervals[bay]]
            if not self.running[bay] and self.stop_start[bay] is not None:
                intervals.append({'stop_minute': self.stop_start[bay],
                                  'resume_minute': None, 'plan': None,
                                  'ongoing_minutes': self.last_minute - self.stop_start[bay]})
            lost = sum(i['ongoing_minutes'] if 'ongoing_minutes' in i
                       else i['resume_minute'] - i['stop_minute'] for i in intervals)
            result[bay] = {'lost_minutes': lost, 'intervals': intervals}
        return result
