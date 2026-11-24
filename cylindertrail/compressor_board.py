"""Current job-by-job tariff choice without shared feeder accounting."""
from .power_tariff import price_at


def plan(jobs, tariff):
    result = []
    for job in jobs:
        choices = [minute for minute in job['start_options'] if minute + job['duration_minutes'] <= job['deadline_minute']]
        start = min(choices, key=lambda minute: price_at(minute, tariff)) if choices else None
        result.append({'job': job['id'], 'start_minute': start,
                       'end_minute': start + job['duration_minutes'] if start is not None else None})
    return result
