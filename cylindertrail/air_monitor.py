"""Combustible gas alarm grading.

A local sensor reading is never the whole safety story of a filling bay, but
the grade derived here is the evidence every higher layer starts from.

The alarm judgement uses the *worst-case upper bound* of each sensor:
``gas_ppm + calibration_error_ppm``. A sensor that is stale (lagging) or
missing cannot be treated as zero concentration — its bay stays unsafe until
a fresh valid sample proves otherwise.
"""

# Reading is valid and even the worst-case upper bound stays below threshold.
NORMAL = 'normal'
# Point value is below threshold, but the calibration uncertainty band crosses
# it: gas_ppm < alarm_ppm <= gas_ppm + calibration_error_ppm.
WARNING = 'warning'
# Point value itself reaches the threshold: gas_ppm >= alarm_ppm.
ALARM = 'alarm'
# Sensor stale/missing/unknown: zero concentration must not be assumed.
FAULT = 'fault'

UNSAFE_GRADES = (ALARM, WARNING, FAULT)


def worst_case_ppm(reading):
    """Worst-case concentration upper bound after calibration uncertainty."""
    error = reading.get('calibration_error_ppm', 0) or 0
    return reading['gas_ppm'] + error


def grade_reading(reading, *, now=None, stale_after_minutes=None):
    """Grade one sensor reading conservatively.

    A reading without ``gas_ppm`` (missing) or flagged stale/lagging is graded
    :data:`FAULT`. Time-based staleness is only applied when the caller passes
    both ``now`` and an explicit ``stale_after_minutes`` window — a sparse
    industrial sample must not turn into a fault merely because no new sample
    happened to arrive; lagging is proven by an explicit status or by the
    caller's chosen window, never assumed.
    """
    status = reading.get('status', 'ok')
    if status in ('missing', 'stale', 'fault', 'lagging') or reading.get('gas_ppm') is None:
        return FAULT
    if (now is not None and stale_after_minutes is not None
            and reading.get('at_minute') is not None):
        if now - reading['at_minute'] > stale_after_minutes:
            return FAULT
    if reading['gas_ppm'] >= reading['alarm_ppm']:
        return ALARM
    if worst_case_ppm(reading) >= reading['alarm_ppm']:
        return WARNING
    return NORMAL


def alarming(reading, *, now=None, stale_after_minutes=None):
    """Whether the sensor's worst-case upper bound reaches its alarm level.

    Backwards-compatible entry point for the single-point baseline: a plain
    reading with no calibration error keeps its old boolean behaviour, while
    error bands, lag and missing samples are all treated as alarming.
    """
    return grade_reading(reading, now=now,
                         stale_after_minutes=stale_after_minutes) in UNSAFE_GRADES
