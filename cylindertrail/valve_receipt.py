"""Per-serial valve receipt inspection.

Receipt is no longer decided by counting boxes.  Every valve body carries one
unique serial number and one real torque test record.  A torque reading is a
measurement, not a fact: the true value may lie anywhere within the instrument
uncertainty, so a unit is only accepted when *the least favourable side* of its
measurement interval still lies inside the contract band (50-60 N.m).
"""

from .supplier_order import contract_limits, ordered_units, torque_uncertainty_nm

QUALIFIED = 'qualified'
QUARANTINED = 'quarantined'


def inspect_valve(valve, minimum, maximum, uncertainty):
    """Return a per-serial inspection result.

    ``valve['torque_nm']`` is the real measured torque.  The accepted
    interval is ``[torque - u, torque + u]``; a unit qualifies only if the
    *entire* interval is within ``[minimum, maximum]``.  No averaging across
    valves and no substitution of another unit's reading is possible because
    the check only ever sees this serial number's own record.
    """
    serial = valve.get('serial')
    if not serial:
        raise ValueError('every valve needs a unique serial number')
    reading = valve.get('torque_nm')
    if reading is None:
        raise ValueError(f'valve {serial} has no torque test record')
    reading = float(reading)
    worst_low = round(reading - uncertainty, 4)
    worst_high = round(reading + uncertainty, 4)
    failures = []
    if worst_low < minimum:
        failures.append('worst-case_low_below_minimum')
    if worst_high > maximum:
        failures.append('worst-case_high_above_maximum')
    qualified = not failures
    return {
        'serial': serial,
        'torque_nm': reading,
        'uncertainty_nm': uncertainty,
        'worst_case_low_nm': worst_low,
        'worst_case_high_nm': worst_high,
        'minimum_nm': minimum,
        'maximum_nm': maximum,
        'qualified': qualified,
        'status': QUALIFIED if qualified else QUARANTINED,
        'failures': failures,
    }


def inspect_shipment(order, valves):
    """Inspect a delivery, keyed by serial number.

    Duplicate serial numbers are rejected outright: two bodies cannot share
    one identity, so re-registering a failed serial as a second good unit is
    impossible.  The same physical valve may be inspected again later
    (rework / replacement); that is handled by the capture engine, which
    decides which inspection record is current.
    """
    minimum, maximum = contract_limits(order)
    uncertainty = torque_uncertainty_nm(order)
    seen = set()
    results = {}
    for valve in valves:
        serial = valve.get('serial')
        if serial in seen:
            raise ValueError(f'duplicate serial number {serial!r} in one delivery')
        seen.add(serial)
        results[serial] = inspect_valve(valve, minimum, maximum, uncertainty)
    return results


def decide(order, valves):
    """Backward-compatible one-word shipment verdict.

    ``accepted`` only when the delivery is complete and every serial passed
    its own worst-case torque test.  Anything else stays ``pending`` (or is
    ``rejected`` when a unit failed), never accepted by count alone.
    """
    results = inspect_shipment(order, valves)
    failed = [r for r in results.values() if not r['qualified']]
    if failed:
        return 'rejected'
    if len(results) != ordered_units(order):
        return 'pending'
    return 'accepted'
