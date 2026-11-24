"""Accept an incoming bottle if a cage has a free slot."""
from .cage import free_slots

def accepted(cage, incoming):
    return free_slots(cage) > 0
