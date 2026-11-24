"""Cross-order station schedule: one shared feeder, shared compressors, replayable plans.

The legacy ``plan`` picks each job's cheapest hour in isolation and is kept as
the seed counterexample: two individually legal jobs can land on the same hour
and overload the feeder. ``plan_station`` instead searches every cross-order
combination against per-minute feeder capacity, compressor rating, minimum run
and cooldown, and tank capacity, so a station-wide plan is only accepted when
it replays cleanly minute by minute.
"""
import math
from itertools import product

from .power_tariff import cost_between, price_at, segments_between


def plan(jobs, tariff):
    """Legacy per-job cheapest pick, kept as the seed counterexample."""
    result = []
    for job in jobs:
        choices = [minute for minute in job['start_options'] if minute + job['duration_minutes'] <= job['deadline_minute']]
        start = min(choices, key=lambda minute: price_at(minute, tariff)) if choices else None
        result.append({'job': job['id'], 'start_minute': start,
                       'end_minute': start + job['duration_minutes'] if start is not None else None})
    return result


def _candidates(job, compressors, now, compressor_available):
    for start in sorted(s for s in job['start_options']
                        if s >= now and s + job['duration_minutes'] <= job['deadline_minute']):
        for comp in compressors:
            if job['power_kw'] > comp['max_kw']:
                continue  # nameplate rating
            if job['duration_minutes'] < comp.get('min_run_minutes', 0):
                continue  # minimum continuous run
            if start < compressor_available.get(comp['id'], 0):
                continue  # still cooling down after the previous stop
            yield {'job': job['id'], 'compressor': comp['id'], 'start_minute': start,
                   'end_minute': start + job['duration_minutes'],
                   'duration_minutes': job['duration_minutes'],
                   'deadline_minute': job['deadline_minute'],
                   'power_kw': job['power_kw'], 'gas_m3': job['gas_m3']}


def replay(assignments, feeder_capacity, compressors=(), tank_capacity_m3=None):
    """Re-simulate a station plan minute by minute and return every violation."""
    violations = []
    if not assignments:
        return violations
    comps = {c['id']: c for c in compressors}
    first = min(a['start_minute'] for a in assignments)
    last = max(a['end_minute'] for a in assignments)
    for minute in range(first, last):
        load = sum(a['power_kw'] for a in assignments if a['start_minute'] <= minute < a['end_minute'])
        limit = feeder_capacity(minute)
        if load > limit:
            violations.append({'kind': 'feeder_overload', 'minute': minute,
                               'load_kw': load, 'limit_kw': limit})
        if tank_capacity_m3 is not None:
            stored = sum(a['gas_m3'] * min(max(minute + 1 - a['start_minute'], 0), a['duration_minutes'])
                         / a['duration_minutes'] for a in assignments)
            if stored > tank_capacity_m3 + 1e-9:
                violations.append({'kind': 'tank_overflow', 'minute': minute,
                                   'stored_m3': round(stored, 6), 'capacity_m3': tank_capacity_m3})
    for a in assignments:
        if a['end_minute'] > a.get('deadline_minute', a['end_minute']):
            violations.append({'kind': 'deadline_missed', 'job': a['job'], 'end_minute': a['end_minute']})
        comp = comps.get(a.get('compressor'))
        if comp is None:
            continue
        if a['power_kw'] > comp['max_kw']:
            violations.append({'kind': 'rating_exceeded', 'job': a['job'], 'compressor': comp['id']})
        if a['end_minute'] - a['start_minute'] < comp.get('min_run_minutes', 0):
            violations.append({'kind': 'min_run_violated', 'job': a['job'], 'compressor': comp['id']})
    for comp in compressors:
        runs = sorted((a for a in assignments if a.get('compressor') == comp['id']),
                      key=lambda a: a['start_minute'])
        for earlier, later in zip(runs, runs[1:]):
            if later['start_minute'] < earlier['end_minute'] + comp.get('cooldown_minutes', 0):
                violations.append({'kind': 'cooldown_violated', 'compressor': comp['id'],
                                   'stopped_at': earlier['end_minute'],
                                   'restarted_at': later['start_minute']})
    return violations


def plan_station(jobs, tariff, feeder_limit_kw, compressors, *, priority=None, now=0,
                 feeder_capacity=None, compressor_available=None, tank_capacity_m3=None):
    """Search every cross-order combination and keep the best station-wide plan.

    Objective: serve as many orders as physically possible, then honour the
    authorised customer priority order (earliest completion first), then the
    lowest metered cost. The order of customers is never hardcoded; if tank
    capacity or equipment state rules one sequence out, the search re-solves.
    """
    capacity = feeder_capacity or (lambda minute: feeder_limit_kw)
    available = dict(compressor_available or {})
    jobs = list(jobs)
    per_job = [list(_candidates(job, compressors, now, available)) + [None] for job in jobs]
    prio = list((priority or {}).get('order', []))
    best, best_key = None, None
    for combo in product(*per_job):
        assignments = [a for a in combo if a is not None]
        if replay(assignments, capacity, compressors, tank_capacity_m3):
            continue
        ends = {a['job']: a['end_minute'] for a in assignments}
        cost = sum(cost_between(a['start_minute'], a['end_minute'], a['power_kw'], tariff)
                   for a in assignments)
        key = (-len(assignments),
               tuple(ends.get(jid, math.inf) for jid in prio),
               round(cost, 9),
               sum(a['start_minute'] for a in assignments),
               tuple(a['compressor'] for a in assignments))
        if best_key is None or key < best_key:
            best, best_key = assignments, key
    assignments = sorted(best, key=lambda a: (a['start_minute'], a['job']))
    scheduled = {a['job'] for a in assignments}
    rows = [{**a, 'cost': cost_between(a['start_minute'], a['end_minute'], a['power_kw'], tariff),
             'segments': segments_between(a['start_minute'], a['end_minute'], tariff)}
            for a in assignments]
    if assignments:
        first = min(a['start_minute'] for a in assignments)
        last = max(a['end_minute'] for a in assignments)
        profile = [{'minute': m,
                    'load_kw': sum(a['power_kw'] for a in assignments
                                   if a['start_minute'] <= m < a['end_minute'])}
                   for m in range(first, last)]
    else:
        profile = []
    unscheduled = [j['id'] for j in jobs if j['id'] not in scheduled]
    return {'feasible': not unscheduled, 'assignments': rows, 'unscheduled': unscheduled,
            'total_cost': sum(r['cost'] for r in rows), 'feeder_profile': profile,
            'priority_version': (priority or {}).get('version')}
