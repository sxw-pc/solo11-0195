"""Current stop command only targets the sensor's own filling bay."""
from .air_monitor import alarming


def commands(readings, bays):
    stopped = {r['bay'] for r in readings if alarming(r)}
    return {bay['id']: 'stop' if bay['id'] in stopped else 'run' for bay in bays}
