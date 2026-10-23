import os
from pathlib import Path

class KillSwitch:
    def __init__(self, path='KILL', env='INCIDENT_ZERO_KILL'):
        self.path, self.env = Path(path), env

    def check(self):
        # Any nonempty value except explicit false is fail-closed.
        try:
            self.path.lstat()
        except FileNotFoundError:
            marker_exists = False
        else:
            marker_exists = True
        # Permission/I/O errors propagate rather than silently treating a marker as absent.
        if marker_exists or os.getenv(self.env, '').lower() not in ('', '0', 'false'):
            raise RuntimeError('Kill switch active')
