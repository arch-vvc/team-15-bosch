"""A small simulated demo for tests: span metrics, traces, containers, logs and docker events.

The default world is "payment fails 50 % of charges" with flag data planted in the places the
real demo leaks it (fault-signatures.md, section 2), so redaction is tested on every run.
"""
from __future__ import annotations

import copy
import re

from agent.tools.backends import Backends

T0 = 1_761_213_600.0            # 2025-10-23 10:00:00 UTC, the "now" of every test
FAULT_AT = T0 - 300             # the fault started 5 minutes before now
MB = 1024 * 1024

SHOP = ["ad", "cart", "checkout", "currency", "email", "frontend", "frontend-proxy", "image-provider",
        "payment", "product-catalog", "quote", "recommendation", "shipping"]


def healthy_services() -> dict:
    s = {svc: {"rate": 2.0, "err": 0.0, "p95": 20.0, "ops": {}} for svc in SHOP}
    s["frontend"]["rate"], s["frontend-proxy"]["rate"] = 10.0, 10.0
    s["payment"]["ops"] = {"oteldemo.PaymentService/Charge": {"rate": 1.0, "err": 0.0, "p95": 15.0}}
    s["checkout"]["ops"] = {"oteldemo.CheckoutService/PlaceOrder": {"rate": 1.0, "err": 0.0, "p95": 120.0},
                            "oteldemo.PaymentService/Charge": {"rate": 1.0, "err": 0.0, "p95": 18.0}}
    return s


def payment_failure_services() -> dict:
    s = healthy_services()
    s["payment"]["err"] = 0.5
    s["payment"]["ops"]["oteldemo.PaymentService/Charge"]["err"] = 0.5
    s["checkout"]["err"] = 0.5
    s["checkout"]["ops"]["oteldemo.CheckoutService/PlaceOrder"]["err"] = 0.5
    s["checkout"]["ops"]["oteldemo.PaymentService/Charge"]["err"] = 0.5
    s["frontend"]["err"] = 0.08
    return s


def _span(sid, parent, op, start_us, dur_us, pid, kind, error=False, tags=(), logs=()):
    t = [{"key": "span.kind", "type": "string", "value": kind}] + list(tags)
    if error:
        t += [{"key": "error", "type": "bool", "value": True}, {"key": "otel.status_code", "type": "string", "value": "ERROR"}]
    return {"traceID": "", "spanID": sid, "operationName": op, "startTime": start_us, "duration": dur_us,
            "processID": pid, "references": [{"refType": "CHILD_OF", "spanID": parent}] if parent else [],
            "tags": t, "logs": list(logs)}


def payment_trace(n: int, start_s: float) -> dict:
    """frontend -> checkout -> payment, payment's Charge fails for a gold customer.
    Also a flag lookup span to flagd and a feature_flag event, which must be dropped."""
    us = int(start_s * 1e6)
    tid = f"{n:032x}"
    spans = [
        _span("a1", None, "POST /api/checkout", us, 900_000, "p1", "server", error=True),
        _span("b1", "a1", "oteldemo.CheckoutService/PlaceOrder", us + 1000, 800_000, "p1", "client", error=True),
        _span("b2", "b1", "oteldemo.CheckoutService/PlaceOrder", us + 2000, 780_000, "p2", "server", error=True,
              tags=[{"key": "app.user.id", "type": "string", "value": f"user-{n}"}]),
        _span("c1", "b2", "oteldemo.PaymentService/Charge", us + 5000, 40_000, "p2", "client", error=True),
        _span("c2", "c1", "oteldemo.PaymentService/Charge", us + 6000, 30_000, "p3", "server", error=True,
              tags=[{"key": "demo.user_context.loyalty_level", "type": "string", "value": "gold"},
                    {"key": "demo.feature_flag.payment", "type": "string", "value": "paymentFailure"}],
              logs=[{"timestamp": us + 7000, "fields": [
                  {"key": "event", "type": "string", "value": "exception"},
                  {"key": "exception.message", "type": "string",
                   "value": "Payment request failed. Invalid token. demo.user_context.loyalty_level=gold"}]},
                  {"timestamp": us + 6500, "fields": [
                      {"key": "event", "type": "string", "value": "feature_flag"},
                      {"key": "feature_flag.key", "type": "string", "value": "paymentFailure"},
                      {"key": "feature_flag.variant", "type": "string", "value": "50%"}]}]),
        _span("d1", "b2", "flagd.evaluation.v1.Service/ResolveFloat", us + 3000, 2_000, "p2", "client",
              tags=[{"key": "peer.service", "type": "string", "value": "flagd"}]),
        _span("e1", "d1", "resolveFloat", us + 3500, 1_000, "p4", "server"),
    ]
    for s in spans:
        s["traceID"] = tid
    return {"traceID": tid, "spans": spans, "warnings": None, "processes": {
        "p1": {"serviceName": "frontend", "tags": []},
        "p2": {"serviceName": "checkout", "tags": []},
        "p3": {"serviceName": "payment", "tags": [{"key": "openfeature.provider", "type": "string", "value": "flagd"}]},
        "p4": {"serviceName": "flagd", "tags": []}}}


def catalog_trace(n: int, start_s: float) -> dict:
    """product-catalog leaking the flag phrase in its status text."""
    us = int(start_s * 1e6)
    tid = f"c{n:031x}"
    span = _span("x1", None, "oteldemo.ProductCatalogService/GetProduct", us, 5_000, "p1", "server", error=True,
                 tags=[{"key": "otel.status_description", "type": "string",
                        "value": "Error: Product Catalog Fail Feature Flag Enabled"},
                       {"key": "app.product.id", "type": "string", "value": "OLJCESPC7Z"}])
    span["traceID"] = tid
    return {"traceID": tid, "spans": [span], "processes": {"p1": {"serviceName": "product-catalog", "tags": []}}}


def container(name, status="running", restarts=0, oom=False, mem_mb=None, nano_cpus=0, started="2025-10-23T09:00:00.123456789Z"):
    from agent import config

    mem_mb = mem_mb if mem_mb is not None else config.services()["containers"][name]["mem_limit_mb"]
    return {"Name": f"/{name}", "RestartCount": restarts,
            "State": {"Status": status, "Running": status == "running", "OOMKilled": oom,
                      "StartedAt": started, "ExitCode": 0 if status == "running" else 137},
            "HostConfig": {"Memory": int(mem_mb * MB), "NanoCpus": nano_cpus, "CpuQuota": 0, "CpuPeriod": 0},
            "NetworkSettings": {"Ports": {}}}


def stats(cores: float, mem_mb: float) -> dict:
    return {"cpu_stats": {"cpu_usage": {"total_usage": int(cores * 1e9) + 10**9}, "system_cpu_usage": 2 * 10**9,
                          "online_cpus": 1},
            "precpu_stats": {"cpu_usage": {"total_usage": 10**9}, "system_cpu_usage": 10**9},
            "memory_stats": {"usage": int((mem_mb + 5) * MB), "stats": {"inactive_file": 5 * MB}}}


class World:
    """Mutable state of the fake demo. Tests change it between polls."""

    def __init__(self):
        from agent import config

        self.now = T0
        self.baseline_services = healthy_services()
        self.services = payment_failure_services()
        self.fault_at = FAULT_AT
        self.containers = {n: container(n) for n in config.all_containers()}
        self.containers["flagd"] = container("flagd", mem_mb=75)
        self.usage = {n: (0.05, 0.4 * c["mem_limit_mb"]) for n, c in config.services()["containers"].items()}
        self.traces = {"frontend": [payment_trace(i, FAULT_AT + 10 + i) for i in range(6)],
                       "checkout": [payment_trace(i, FAULT_AT + 10 + i) for i in range(6)],
                       "payment": [payment_trace(i, FAULT_AT + 10 + i) for i in range(6)],
                       "product-catalog": [catalog_trace(i, FAULT_AT + 20 + i) for i in range(2)]}
        self.edges = [{"parent": "frontend", "child": "checkout", "callCount": 50},
                      {"parent": "checkout", "child": "payment", "callCount": 40},
                      {"parent": "frontend", "child": "product-catalog", "callCount": 90},
                      {"parent": "checkout", "child": "flagd", "callCount": 90}]
        self.baseline_edges = list(self.edges)
        self.logs = {
            "payment": [f"2025-10-23T09:55:{15 + 5 * i}.000000000Z {{\"level\":\"error\",\"msg\":\"Payment "
                        f"request failed. Invalid token. order {1000 + i}\"}}" for i in range(6)]
                       + ["2025-10-23T09:54:30.000000000Z {\"level\":\"info\",\"msg\":\"charged card\"}"],
            "ad": ["2025-10-23T09:56:00.000000000Z WARN Feature Flag adManualGc enabled, performing a manual gc now"],
            "shipping": ["2025-10-23T09:57:00.000000000Z INFO Delaying international shipment due to "
                         "intlShippingSlowdown feature flag feature_flag_key=intlShippingSlowdown"],
        }
        self.events: list[dict] = []


class FakeProm:
    def __init__(self, world: World):
        self.w = world

    def _value(self, services: dict, expr: str) -> dict[str, float]:
        by = re.search(r"sum by \((?:le, )?(\w+)\)", expr)
        by = by.group(1) if by else None
        svc = re.search(r'service_name="([^"]+)"', expr)
        is_err, is_p95 = "STATUS_CODE_ERROR" in expr, "histogram_quantile" in expr
        out = {}
        for name, s in services.items():
            if svc and name != svc.group(1):
                continue
            rows = s["ops"].items() if by == "span_name" else [(name, s)]
            for key, r in rows:
                if r["rate"] <= 0:
                    continue
                out[key] = r["p95"] if is_p95 else r["rate"] * (r["err"] if is_err else 1.0)
        return out

    def query(self, expr: str, at: float | None = None) -> list[dict]:
        services = self.w.baseline_services if at is not None and at < self.w.fault_at else self.w.services
        by = re.search(r"sum by \((?:le, )?(\w+)\)", expr)
        label = by.group(1) if by else "service_name"
        rows = [{"metric": {label: k}, "value": [self.w.now, str(v)]} for k, v in self._value(services, expr).items()]
        # calls to and from flagd show up as their own series in real span metrics
        rows.append({"metric": {label: "flagd" if label == "service_name" else "flagd.evaluation.v1.Service/ResolveFloat"},
                     "value": [self.w.now, "3"]})
        return rows

    def query_range(self, expr: str, start: float, end: float, step: float) -> list[dict]:
        svc = re.search(r'service_name="([^"]+)"', expr).group(1)
        values, t = [], start
        while t <= end:
            services = self.w.baseline_services if t < self.w.fault_at + 60 else self.w.services
            s = services[svc]
            if s["rate"] > 0:
                if " / " in expr:
                    v = s["err"]
                elif "histogram_quantile" in expr:
                    v = s["p95"]
                else:
                    v = s["rate"]
                values.append([t, str(v)])
            t += step
        return [{"metric": {}, "values": values}] if values else []


class FakeJaeger:
    def __init__(self, world: World):
        self.w = world

    def traces(self, service, operation=None, tags=None, lookback="15m", limit=20, start_us=None, end_us=None):
        if start_us is not None:      # a baseline window: healthy, fast traces
            return [self._healthy_hop(i, start_us / 1e6 + i) for i in range(5)]
        return copy.deepcopy(self.w.traces.get(service, []))[:limit]

    def _healthy_hop(self, n, start_s):
        t = payment_trace(n, start_s)
        for s in t["spans"]:
            for tag in list(s["tags"]):
                if tag["key"] in ("error", "otel.status_code"):
                    s["tags"].remove(tag)
        return t

    def dependencies(self, end_ms, lookback_ms):
        edges = self.w.baseline_edges if end_ms / 1000 < self.w.fault_at else self.w.edges
        return copy.deepcopy(edges)

    def services(self):
        return sorted(self.w.traces)


class FakeDocker:
    def __init__(self, world: World):
        self.w = world

    def inspect(self, name):
        return copy.deepcopy(self.w.containers.get(name))

    def stats(self, name):
        c = self.w.containers.get(name)
        if not c or c["State"]["Status"] != "running":
            return None
        return stats(*self.w.usage[name])

    def logs(self, name, since, tail):
        return list(self.w.logs.get(name, []))[-tail:]

    def events(self, since, until):
        return [e for e in self.w.events if since <= e["time"] <= until]

    def host_port(self, name, port):
        return None


class FakeClock:
    def __init__(self, world: World):
        self.w = world

    def now(self) -> float:
        return self.w.now


def backends(world: World | None = None) -> tuple[Backends, World]:
    w = world or World()
    return Backends(prom=FakeProm(w), jaeger=FakeJaeger(w), docker=FakeDocker(w), opensearch=None,
                    clock=FakeClock(w)), w


def docker_event(name: str, action: str, ts: float, **attrs) -> dict:
    return {"Type": "container", "Action": action, "status": action, "time": int(ts), "timeNano": int(ts * 1e9),
            "Actor": {"ID": "abc", "Attributes": {"name": name, "com.docker.compose.service": name, **attrs}}}
