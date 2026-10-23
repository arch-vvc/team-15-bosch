import os
from pathlib import Path

class KillSwitch:
    def __init__(self, path='KILL', env='INCIDENT_ZERO_KILL'):
        self.path, self.env = Path(path), env

    def check(self):
        # Any nonempty value except explicit false is fail-closed.
        if self.path.exists() or os.getenv(self.env, '').lower() not in ('', '0', 'false'):
            raise RuntimeError('Kill switch active')
