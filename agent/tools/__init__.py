"""The Eyes toolbox: every read-only tool, bound to one set of backends, one ledger and one
baseline. Each call stores its evidence in the ledger and returns it with IDs assigned,
so anything the rules, the LLM or the report cite can be checked later.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from agent.contracts import Evidence
from agent.ledger import Ledger
from agent.tools import deploy_history as _deploy
from agent.tools import docker_state, jaeger, logs, prometheus
from agent.tools.backends import Backends, BackendError

if TYPE_CHECKING:
    from agent.baseline import Baseline


class Toolbox:
    def __init__(self, backends: Backends, ledger: Ledger, baseline: Baseline | None = None):
        self.b, self.ledger, self.baseline = backends, ledger, baseline

    def record(self, tool: str, query: dict, fn) -> list[Evidence]:
        """Run a tool; a backend failure becomes an empty evidence item that says so,
        instead of an exception, so one dead backend never stops an investigation."""
        try:
            items = fn()
        except BackendError as e:
            items = [Evidence(tool=tool, query=query | {"error": str(e)}, ts=self.b.now_dt(), facts=[])]
        for ev in items:
            self.ledger.add(ev)
        return items

    @property
    def _base_services(self) -> dict:
        return self.baseline.services if self.baseline else {}

    @property
    def _base_containers(self) -> dict:
        return self.baseline.containers if self.baseline else {}

    @property
    def _base_at(self) -> float | None:
        return self.baseline.taken_at if self.baseline else None

    # --- the tools ---------------------------------------------------------------------

    def service_health_overview(self) -> list[Evidence]:
        return self.record("service_health_overview", {},
                           lambda: prometheus.service_health_overview(self.b, self._base_services))

    def service_detail(self, service: str) -> list[Evidence]:
        return self.record("service_detail", {"service": service},
                           lambda: prometheus.service_detail(self.b, service, self._base_at))

    def hop_latency(self, caller: str, callee: str) -> list[Evidence]:
        return self.record("hop_latency", {"caller": caller, "callee": callee},
                           lambda: jaeger.hop_latency(self.b, caller, callee, baseline_at=self._base_at))

    def search_traces(self, service: str, errors_only: bool = True, operation: str | None = None,
                      lookback: str | None = None) -> list[Evidence]:
        return self.record("search_traces", {"service": service},
                           lambda: jaeger.search_traces(self.b, service, errors_only, operation, lookback))

    def container_health(self, service: str | None = None) -> list[Evidence]:
        return self.record("container_health", {"service": service},
                           lambda: docker_state.container_health(self.b, service, self._base_containers))

    def search_logs(self, service: str, pattern: str | None = None, level: str | None = None,
                    since_min: int | None = None) -> list[Evidence]:
        return self.record("search_logs", {"service": service},
                           lambda: logs.search_logs(self.b, service, pattern, level, since_min))

    def onset(self, service: str, signal: str, direction: str = "up") -> list[Evidence]:
        base = self._base_services.get(service, {}).get("req_rate" if direction == "absent" else signal) or 0.0
        return self.record("onset", {"service": service, "signal": signal},
                           lambda: prometheus.onset(self.b, service, signal, base, direction))

    def service_dependencies(self) -> list[Evidence]:
        edges = self.baseline.edges if self.baseline else None
        return self.record("service_dependencies", {},
                           lambda: jaeger.service_dependencies(self.b, healthy_edges=edges))

    def deploy_history(self, service: str | None = None, lookback_min: int | None = None) -> list[Evidence]:
        return self.record("deploy_history", {"service": service},
                           lambda: _deploy.deploy_history(self.b, lookback_min, service))
