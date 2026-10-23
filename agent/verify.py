"""Recovery check (step 12). After an action, re-poll the alerting signal until it has been
normal for `healthy_points` polls in a row, or the window runs out.

remaining_symptoms lists everything else still abnormal at the end, so the orchestrator can
re-triage for a second fault (combined incidents) within the 2-action cap.
"""
from __future__ import annotations

import time
from collections.abc import Callable

from agent import config, detect
from agent.contracts import Alert, VerifyResult
from agent.tools import Toolbox, docker_state
from agent.tools.backends import BackendError


def check(tb: Toolbox, alert: Alert, window_s: int | None = None,
          sleep: Callable[[float], None] = time.sleep,
          on_poll: Callable[[bool, list[Alert]], None] | None = None) -> VerifyResult:
    cfg = config.thresholds()["verify"]
    window_s = window_s or cfg["window_s"]
    baseline = tb.baseline.model_copy(deep=True)
    # restarts are judged from now on: a restart done as the fix is not a new symptom
    try:
        for c in docker_state.container_table(tb.b, baseline.containers, with_usage=False):
            baseline.containers.setdefault(c.service, {})["restarts"] = c.restarts
    except BackendError:
        pass
    key = (alert.service, alert.signal)
    deadline = tb.b.now() + window_s
    max_polls = window_s // cfg["poll_s"] + 1   # also bounds replays, where the clock stands still
    healthy, last, polls = 0, [], 0
    while polls < max_polls:
        polls += 1
        try:
            last = detect.poll(tb.b, baseline)
        except BackendError:
            last = None
        if last is not None:
            ok = key not in {(a.service, a.signal) for a in last}
            healthy = healthy + 1 if ok else 0
            if on_poll:
                on_poll(ok, last)
            if healthy >= cfg["healthy_points"]:
                break
        if tb.b.now() >= deadline:
            break
        sleep(cfg["poll_s"])
    recovered = healthy >= cfg["healthy_points"]
    evidence = tb.service_health_overview() + tb.container_health(alert.service)
    remaining = sorted({f"{a.service}:{a.signal}" for a in (last or [])
                        if not (recovered and (a.service, a.signal) == key)})
    return VerifyResult(recovered=recovered, evidence_ids=[ev.id for ev in evidence], remaining_symptoms=remaining)
