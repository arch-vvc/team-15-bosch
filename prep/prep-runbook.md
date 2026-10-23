# Incident Zero: prep session runbook

Step-by-step for the first session with the demo running. It fills in `fault-signatures.md` (observation template, section 7) and confirms `query-cookbook.md`. **Total time about 3–4 hours**; split the faults across teammates' laptops if you can.

Shorthand used below:
```bash
cd ~/Desktop/opentelemetry-demo
dc() { docker compose --env-file .env --env-file .env.override -f compose.yaml -f compose.observability.yaml -f compose.extras.yaml "$@"; }
```
Run the `dc() …` line once in each new terminal. It defines a `dc` shortcut that works in zsh, the macOS default shell.

---

## 0. Start (10 min)
1. `make start-minimal`, then wait about 2 minutes.
2. Check that everything is up: `dc ps --format "table {{.Name}}\t{{.Status}}"`. Everything should be `Up`. Note any `Restarting`.
3. Open:
   - the shop: http://localhost:8080
   - Jaeger: http://localhost:8080/jaeger/ui
   - Grafana: http://localhost:8080/grafana/
   - the load generator: http://localhost:8080/loadgen/
   - the flag page, for prep only: http://localhost:8080/feature
4. Check memory pressure: `docker stats --no-stream`. If the Mac starts swapping heavily, stop OpenSearch with `docker stop opensearch` and use `docker logs` instead.

## 1. Verify the plumbing (15 min)
Work through the **Live verification checklist** at the end of `query-cookbook.md`, and write the real metric and field names back into that file.

## 2. Baseline (10 min, with no faults)
- Let the load generator run untouched for 10 minutes.
- Record per service: request rate, error ratio and p95 (cookbook Tool 1, `[10m]` window).
- Record the `RestartCount` of every container:
  ```bash
  for c in $(dc ps -q); do docker inspect -f '{{.Name}} {{.RestartCount}}' $c; done
  ```
- Note any service that errors **even when healthy**. That's background noise the thresholds must ignore.

## 3. Application faults (about 10 min each)
**Procedure for every fault:**
1. Note the clock time.
2. Set the flag on the flag page.
3. Watch for **5 minutes**. Fill in the observation template: first signal, strongest signal, knock-on, misleading signals, container state, log lines, the queries that worked, and **whether any flag text appeared**.
4. Optionally try a `docker restart <service>` and note whether it actually fixes the problem.
5. Set the flag back to `off`, and note **how long until metrics return to baseline**.
6. Wait until everything is back to baseline before the next fault.

| # | Flag | Setting | Watch especially |
|---|---|---|---|
| 1 | paymentFailure | 50%, then 100% | payment vs. checkout error ratios; the `loyalty_level=gold` pattern |
| 2 | paymentUnreachable | on | payment Charge server spans dropping to 0; error text address |
| 3 | cartFailure | 50% | only EmptyCart errors? does checkout really succeed? |
| 4 | productCatalogFailure | on | error concentration on one `demo.product.id`; redacted status text |
| 5 | productCatalogLockContention | on | DB latency sawtooth; errors vs. timeouts; knock-on to recommendation |
| 6 | recommendationCacheFailure | on | memory growth rate; time to OOM; restart cycle |
| 7 | adHighCpu | on | CPU absolute value; GetAds p95; do other services slow down? |
| 8 | adManualGc | on | GC metric name and values; heap level |
| 9 | adFailure | on | ~10% GetAds errors; frontend impact (should be minimal) |
| 10 | emailMemoryLeak | 100x (then 10000x, briefly) | memory slope vs. checkout rate; OOM restart |
| 11 | loadGeneratorFloodHomepage | on | request-rate jump comes **first**; which services look sick |
| 12 | intlShippingSlowdown | 5sec | bimodal shipping latency; flag text in shipping logs |
| 13 | failedReadinessProbe | on | any visible effect at all under Compose? |
| 14 | imageSlowLoad | 5sec | open the shop in a browser; visible in proxy spans? |

## 4. Infrastructure faults (about 10 min each)
Same procedure as section 3, but inject with Docker. **Always run the reset line.**

| # | Fault | Inject | Reset |
|---|---|---|---|
| A | Container stopped | `docker stop payment` | `docker start payment` |
| B | Crash/restart loop | `docker kill payment`, three times, 30 s apart | (auto-restarts) |
| C | Cache down | `docker stop valkey-cart` | `docker start valkey-cart` |
| D | Database down | `docker stop astronomy-db` | `docker start astronomy-db` |
| E | CPU starvation | `docker update --cpus 0.05 ad` | `dc up -d --force-recreate ad` |
| F | Memory starvation | `docker update --memory 60m --memory-swap 60m recommendation` | `dc up -d --force-recreate recommendation` |
| G | Network latency | `docker run --rm --net container:shipping --cap-add NET_ADMIN nicolaka/netshoot tc qdisc add dev eth0 root netem delay 500ms` | `dc up -d --force-recreate shipping` |
| H | Packet loss | same as G with `netem loss 30%` | same as G |

- **G and H** pull the `nicolaka/netshoot` image (a few hundred MB) the first time. They delay *all* of shipping's traffic, not just one hop. The organisers' version may be narrower.
- For **E**, also note whether a CPU-throttling metric exists. For **E and F**, check that `docker inspect` shows the changed limit.
- **Watch for C vs. cartFailure, D vs. productCatalogFailure, A vs. paymentUnreachable, E vs. adHighCpu, F vs. the recommendation leak.** Those pairs are the core of the competing-hypotheses rules.

## 5. Combinations (about 15 min each, if time allows)
1. paymentFailure 50% + adHighCpu
2. recommendationCacheFailure + productCatalogLockContention
3. `docker stop valkey-cart` + paymentFailure 50%
4. loadGeneratorFloodHomepage + adHighCpu
5. `docker stop astronomy-db` + productCatalogFailure. Restore the DB first, and check that the one-product failure is still visible.

For each, note **which fault shows first** and **which fault hides the other**.

## 6. Wrap up
- Reset everything: turn all flags `off`, then `make start-minimal`, which recreates every container.
- `make stop` when done, to free RAM.
- Fill the **signature table** for P3. One row per fault: `signals that must be present`, `signals that must be absent`, `discriminating check`, `typical onset order`, `fix that worked`.
- List every place flag text appeared, for P1's redaction tests.

## Safety notes
- All of this runs on your own laptop only. Never point it at anything else.
- The flag page is fine for **prep**. On the day, nobody (and no agent) reads flag state to diagnose.
- None of this is project code. Write the actual agent only on the day.
