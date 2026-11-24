"""Current discharge verdict from an unphysical mean pH."""
from .effluent_sample import mean_ph


def decide(tanks, lower=6, upper=9):
    ph = mean_ph(tanks)
    return 'discharge' if lower <= ph <= upper else 'hold'
