"""Current cumulative training hours without attendance interval accounting."""


def hours(sessions):
    return sum(row['credited_hours'] for row in sessions)
