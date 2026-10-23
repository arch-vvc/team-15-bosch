import json
import os
from datetime import datetime, timezone

class ActionLog:
    """Write-ahead audit: failure prevents the call; no truncation or rewriting."""
    def __init__(self, path='action_log.jsonl'):
        self.path = path

    def append(self, **record):
        data = (json.dumps({'ts': datetime.now(timezone.utc).isoformat(), **record},
                           default=lambda o: o.model_dump(mode='json')) + '\n').encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise OSError('Incomplete audit write')
            os.fsync(fd)
        finally:
            os.close(fd)
