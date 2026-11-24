"""Report free bottle slots in a depot cage."""

def free_slots(cage):
    return cage['capacity'] - len(cage['bottles'])
