"""Current local fill authorization from a training-hour threshold."""
from .training_credit import hours


def decide(operator, minimum_hours=40):
    return 'authorized' if hours(operator['sessions']) >= minimum_hours else 'trainee'
