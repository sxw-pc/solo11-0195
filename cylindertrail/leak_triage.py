"""Flag a returned bottle based on apparent pressure loss."""
from .pressure import drop

def status(readings):
    return 'isolate' if drop(readings) > 5 else 'normal_storage'
