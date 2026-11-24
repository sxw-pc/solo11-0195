"""Executed-energy and produced-gas ledger: evidence in, immutable records, replans out.

Only cubic meters proven by pressure, flow and tank-margin evidence are booked
against an order. Once metered, consumed kWh and tanked gas are physical facts:
mid-flight deratings, tariff revisions or shutdowns replan only the unexecuted
remainder, executed records keep the price that was metered at the time, and
undeliverable promises are revoked with a minimal conflict interval instead of
being silently overloaded or re-priced as final-price times total energy.
"""
import math

from .compressor_board import plan_station
from .power_tariff import energy_kwh, segments_between

REQUIRED_EVIDENCE = ('pressure_kpa', 'flow_m3h', 'tank_margin_m3')


class Capture:
    def __init__(self, config):
        self.jobs = {j['id']: dict(j) for j in config['jobs']}
        self.tariff = [dict(r) for r in config['tariff']]
        self.feeder_limit_kw = config['feeder_limit_kw']
        self.compressors = [dict(c) for c in config['compressors']]
        self.tank_capacity_m3 = config.get('tank', {}).get('capacity_m3')
        self.deratings = []
        self.revisions = []
        self.executed = []
        self.gas_m3 = {jid: 0.0 for jid in self.jobs}
        self.vented_m3 = {jid: 0.0 for jid in self.jobs}
        self.compressor_free_at = {c['id']: 0 for c in self.compressors}
        self.priority = None

    # --- evidence in -----------------------------------------------------
    def record_execution(self, job_id, compressor_id, start, end, power_kw, gas_m3, evidence):
        """Book an actually metered run; gas without full evidence does not count."""
        job = self.jobs[job_id]
        missing = [k for k in REQUIRED_EVIDENCE if k not in (evidence or {})]
        if missing:
            raise ValueError(f'metered gas needs pressure/flow/tank-margin evidence, missing {missing}')
        if end <= start or gas_m3 < 0:
            raise ValueError('execution window and gas amount must be positive')
        rate = job['gas_m3'] / job['duration_minutes']
        if gas_m3 > rate * (end - start) + 1e-9:
            raise ValueError('claimed gas exceeds the physical output of the run')
        if evidence['tank_margin_m3'] < gas_m3:
            raise ValueError('tank had no margin for the claimed gas')
        comp = next(c for c in self.compressors if c['id'] == compressor_id)
        if power_kw > comp['max_kw']:
            raise ValueError('compressor rating exceeded')
        if start < self.compressor_free_at[compressor_id]:
            raise ValueError('compressor still in cooldown')
        pieces = segments_between(start, end, self.tariff)
        record = {'job': job_id, 'compressor': compressor_id, 'start_minute': start,
                  'end_minute': end, 'power_kw': power_kw,
                  'kwh': energy_kwh(power_kw, end - start), 'gas_m3': gas_m3,
                  'segments': pieces,
                  'cost': sum(energy_kwh(power_kw, p['end_minute'] - p['start_minute'])
                              * p['price_per_kwh'] for p in pieces),
                  'evidence': dict(evidence)}
        self.executed.append(record)
        self.gas_m3[job_id] += gas_m3
        self.compressor_free_at[compressor_id] = end + comp.get('cooldown_minutes', 0)
        return record

    def record_shutdown(self, job_id, at_minute, vented_m3=0):
        """Vented gas is physically lost; it is tracked so it is never booked as produced."""
        self.vented_m3[job_id] += vented_m3
        return {'job': job_id, 'at_minute': at_minute, 'vented_m3': vented_m3}

    def derate(self, from_minute, limit_kw):
        event = {'kind': 'derate', 'from_minute': from_minute, 'limit_kw': limit_kw}
        self.deratings.append(event)
        return event

    def revise_tariff(self, rows, effective_minute):
        """Re-price only energy metered after the revision; executed records stay frozen."""
        self.revisions.append({'effective_minute': effective_minute,
                               'rows': [dict(r) for r in rows]})
        self.tariff = [dict(r) for r in rows]
        return self.revisions[-1]

    # --- state views -----------------------------------------------------
    def capacity_at(self, minute):
        active = [d for d in self.deratings if d['from_minute'] <= minute]
        if not active:
            return self.feeder_limit_kw
        return max(active, key=lambda d: d['from_minute'])['limit_kw']

    def executed_kwh(self):
        return sum(r['kwh'] for r in self.executed)

    def executed_cost(self):
        return sum(r['cost'] for r in self.executed)

    def remaining_demand(self, job_id):
        return self.jobs[job_id]['gas_m3'] - self.gas_m3[job_id]

    # --- replan ----------------------------------------------------------
    def replan(self, now, priority=None):
        """Replan the unexecuted remainder; executed energy and tanked gas stay put."""
        if priority is not None and self.priority is not None \
                and priority.get('order') != self.priority.get('order'):
            authorised = priority.get('authorized_by') and \
                priority.get('version', 0) > self.priority.get('version', 0)
            if not authorised:
                raise PermissionError('customer priority change requires an authorized version bump')
        if priority is not None:
            self.priority = dict(priority)
        jobs = []
        for job in self.jobs.values():
            remaining = job['gas_m3'] - self.gas_m3[job['id']]
            if remaining <= 1e-9:
                continue
            started = any(r['job'] == job['id'] for r in self.executed)
            if not started:
                jobs.append({**job, 'start_options': [s for s in job['start_options'] if s >= now]})
            else:
                rate = job['gas_m3'] / job['duration_minutes']
                duration = math.ceil(remaining / rate - 1e-9)
                jobs.append({**job, 'duration_minutes': duration, 'gas_m3': remaining,
                             'start_options': list(range(now, job['deadline_minute'] - duration + 1))})
        tank_remaining = None
        if self.tank_capacity_m3 is not None:
            tank_remaining = self.tank_capacity_m3 - sum(self.gas_m3.values())
        station = plan_station(jobs, self.tariff, self.feeder_limit_kw, self.compressors,
                               priority=self.priority, now=now,
                               feeder_capacity=self.capacity_at,
                               compressor_available=self.compressor_free_at,
                               tank_capacity_m3=tank_remaining)
        revoked = list(station['unscheduled'])
        return {'feasible': not revoked,
                'assignments': station['assignments'],
                'revoked': revoked,
                'conflict_interval': self._conflict_interval(jobs, station, now) if revoked else None,
                'feeder_profile': station['feeder_profile'],
                'executed_kwh': self.executed_kwh(),
                'executed_cost': self.executed_cost(),
                'planned_cost': station['total_cost'],
                'total_cost': self.executed_cost() + station['total_cost'],
                'gas_m3': dict(self.gas_m3),
                'vented_m3': dict(self.vented_m3),
                'priority_version': (self.priority or {}).get('version')}

    def _conflict_interval(self, jobs, station, now):
        """Smallest window in which the residual demand cannot physically fit."""
        planned = {}
        for a in station['assignments']:
            for minute in range(a['start_minute'], a['end_minute']):
                planned[minute] = planned.get(minute, 0) + a['power_kw']
        spans = []
        for job in jobs:
            if job['id'] not in station['unscheduled']:
                continue
            bad = [m for m in range(now, job['deadline_minute'])
                   if self.capacity_at(m) - planned.get(m, 0) < job['power_kw']]
            spans.append((min(bad), max(bad) + 1) if bad else (now, job['deadline_minute']))
        if not spans:
            return None
        return (min(s for s, _ in spans), max(e for _, e in spans))
