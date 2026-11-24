"""Current all-site handover gate from the mean outlet pressure."""
from .regulator_test import mean_bar


def decide(site, readings):
    mean = mean_bar(readings)
    return 'commissioned' if site['minimum_bar'] <= mean <= site['maximum_bar'] else 'hold'
