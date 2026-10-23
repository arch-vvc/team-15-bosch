# Incident Zero: fault signature reference

Prep notes for P1 (tools and queries) and P3 (rule signatures). Not project code.

- **Source:** read from the OpenTelemetry Demo source on 23 Oct 2026 (the `latest` images in `~/Desktop/opentelemetry-demo`).
- **Status:** every signal below is **expected from reading the code** and still needs to be **confirmed live**. Tick the "Seen live" box during the prep run and correct anything that differs.
- **Rule reminder:** these notes describe what failures *look like in telemetry*. The agent must never read flagd. Rules must encode telemetry patterns, never flag names or incident IDs.

---

## 1. Where the signals come from

### Pipelines and resolution
| Signal | Backend | Resolution | Notes |
|---|---|---|---|
| Traces | Jaeger | ms timestamps | **Best source for timing.** Use span start times for onset and precedence, not metric buckets. |
| Span metrics (calls, errors, latency per service and operation) | Prometheus (via the collector's `span_metrics` connector) | Follows trace flow, ~seconds | Error rate and p95 latency for every service from one family of metrics. |
| App SDK metrics (JVM, runtime, rpc/http durations) | Prometheus (OTLP push) | SDK export interval, **likely 60 s** (not set in `.env`, so the default) | Coarse. Fine for trends, poor for precedence. |
| Container stats (CPU, memory, network) | Prometheus (`docker_stats` receiver) | 10 s | Covers running containers only. **A stopped container stops reporting**, so missing series is itself a signal. |
| Valkey (cache), Postgres (DB), nginx (images), frontend-proxy health check | Prometheus (`redis`, `postgresql`, `nginx`, `http_check` receivers) | 10 s | Health of the data stores without touching them. |
| Logs | OpenSearch, or `docker logs <service>` | Per line | `docker logs` is lighter and enough on 8 GB laptops. |
| Container state | Docker API (`docker inspect`) | Instant | `State.Status`, `RestartCount`, `State.OOMKilled`, `HostConfig.Memory`, `HostConfig.NanoCpus`. **Restarts are not exported as a metric**, so use inspect. |

### Metric names (confirmed from the demo's Grafana dashboards)
- `traces_span_metrics_calls_total{service_name, span_name, span_kind, status_code}`. Errors have `status_code="STATUS_CODE_ERROR"`.
- `traces_span_metrics_duration_milliseconds_bucket{service_name, span_name, …}` for p95 and p99 latency.
- `rpc_server_duration_milliseconds_*` and `rpc_client_duration_milliseconds_*` for gRPC. The client vs. server split is **key for network faults**.
- `http_server_request_duration_seconds_*` and `http_client_request_duration_seconds_*`.
- `db_client_operation_duration_seconds_*` (product-catalog to Postgres).
- `jvm_memory_used_bytes{jvm_memory_type}` (ad, recommendation); JVM GC metrics are likely `jvm_gc_duration_*` (**verify the name live**).
- `process_cpu_seconds_total`, `process_memory_rss_bytes`.
- `postgresql_backends`, `postgresql_blks_hit_total`, etc.
- `nginx_requests_total`, `nginx_connections_current`.
- **Container metrics:** names come from the docker_stats receiver through OTLP translation, likely `container_cpu_utilization_ratio`, `container_memory_usage_total_bytes`, `container_memory_percent_ratio`, `container_uptime_seconds`, labelled by `container_name`. **Verify the exact names live** with `curl -s localhost:9090/api/v1/label/__name__/values | grep container`.

### Starter queries (to test live)
```promql
# error ratio per service (5 min window)
sum by (service_name) (rate(traces_span_metrics_calls_total{status_code="STATUS_CODE_ERROR"}[5m]))
  / sum by (service_name) (rate(traces_span_metrics_calls_total[5m]))

# error ratio per operation within one service
sum by (span_name) (rate(traces_span_metrics_calls_total{service_name="payment",status_code="STATUS_CODE_ERROR"}[5m]))

# p95 latency per service
histogram_quantile(0.95, sum by (le, service_name) (rate(traces_span_metrics_duration_milliseconds_bucket[5m])))

# request rate per service (traffic floods, services going silent)
sum by (service_name) (rate(traces_span_metrics_calls_total{span_kind="SPAN_KIND_SERVER"}[1m]))

# network check: client-side vs server-side gRPC latency for one hop
histogram_quantile(0.95, sum by (le) (rate(rpc_client_duration_milliseconds_bucket{service_name="checkout",rpc_service=~".*ShippingService"}[5m])))
histogram_quantile(0.95, sum by (le) (rate(rpc_server_duration_milliseconds_bucket{service_name="shipping"}[5m])))
```
- **Jaeger (through the proxy):**
  - `GET localhost:8080/jaeger/ui/api/services`
  - `GET …/api/traces?service=checkout&tags={"error":"true"}&limit=20&lookback=15m`
  - `GET …/api/dependencies?endTs=<ms>&lookback=<ms>`
- **Prometheus API:**
  - `GET localhost:9090/api/v1/query?query=…`
  - `GET …/query_range?…` for onset times.

### Container memory limits (from `compose.yaml`; all `restart: unless-stopped`)
| Service | Limit | Service | Limit |
|---|---|---|---|
| recommendation | 500M | payment | 140M |
| ad | 300M | image-provider | 120M |
| frontend | 250M | email | 100M |
| cart | 160M | frontend-proxy | 90M |
| checkout, currency, product-catalog, shipping, valkey-cart | **20M** | astronomy-db | 80M |

When a leak reaches the limit, the container is **OOM-killed and restarted automatically**. You'll see `OOMKilled=true`, `RestartCount` going up, and uptime resetting. Memory drops, then climbs again.

---

## 2. Flag data inside the telemetry (redaction list for P1)

**Disqualification risk.** The demo writes feature-flag information into its own traces and logs. P1's tools must strip all of this before anything reaches the evidence ledger or the LLM.

| Where | What leaks | Source |
|---|---|---|
| recommendation traces | OpenFeature `TracesHook` span events named `feature_flag` (flag key, variant, provider) | `recommendation/.../FeatureFlagConfiguration.java` |
| recommendation traces | attribute `demo.feature_flag.recommendation_cache` | `telemetry-schema/attributes/feature_flag.yaml` |
| product-catalog traces | span status and event text **"Error: Product Catalog Fail Feature Flag Enabled"** | `product-catalog/main.go` |
| ad logs | WARN **"Feature Flag adManualGc enabled, performing a manual gc now"** | `ad/.../AdService.java` |
| shipping logs | event `shipping.feature_flag.evaluated` with `feature_flag_key`, `feature_flag_variant`, `feature_flag_provider_name`; message **"Delaying international shipment due to intlShippingSlowdown feature flag"**; `shipping.feature_flag.evaluation_failed` | `shipping/src/shipping_service.rs` |
| flagd, flagd-ui | the flag service's own traces, metrics and logs | services `flagd`, `flagd-ui` |
| any service | OpenFeature or flagd provider errors in logs, for example checkout "Error creating flagd provider" | `checkout/main.go` |
| load-generator | it fetches flag values from flagd (`get_flagd_value`); any spans or logs of those calls | `load-generator/locustfile.py` |

**Redaction rule (applies to every tool output):**
1. Drop span events named `feature_flag`.
2. Drop any attribute whose key contains `feature_flag`.
3. Exclude the services `flagd` and `flagd-ui`.
4. In any message, log line or status text, mask matches of `(?i)feature[ _.-]?flag|flagd|openfeature` and the 20 flag key names (`adFailure`, `adHighCpu`, …, `recommendationCacheFailure`) to `[redacted]`.
5. **Test:** fail the build if any tool output contains `feature_flag`, `flagd` or a flag key name.

Masking keeps the useful part. For example, "Error: Product Catalog Fail [redacted]" still says GetProduct failed with Internal for product X.

---

## 3. Application faults (built-in failure flags)

Columns:
- **First signal:** what moves first.
- **Strongest signal:** what proves it.
- **Knock-on:** where blame could wrongly land.
- **Discriminator:** what separates it from look-alikes.
- **Fix:** which remedy class *could* work. ⚠ marks faults that a restart does **not** fix: only turning off the cause would, so if flag toggling is not allowed, the correct outcome is to escalate.

### 3.1 Payment fails when charging (`paymentFailure`, 10–100%)
- **What happens:** payment's `Charge` throws "Payment request failed. Invalid token. demo.user_context.loyalty_level=gold" for n% of calls.
- **First signal:** payment server span errors on `Charge` (error ratio ≈ n).
- **Strongest signal:** payment server spans with ERROR status and that exception message. **Every failing span has `demo.user_context.loyalty_level=gold`**, a strong fingerprint.
- **Knock-on:** checkout `PlaceOrder` fails ("failed to charge card: could not charge the card: rpc error…", gRPC Internal); frontend `/api/checkout` returns 500. **Checkout looks broken but is only a victim.**
- **Misleading:** the checkout error rate equals the payment error rate, so "deepest failing service" works here. Payment's container is healthy, with normal CPU and memory.
- **Discriminator:** payment *receives* the calls and fails them, so server spans exist with errors. That separates it from "unreachable" and "container down".
- **Fix:** ⚠ a restart won't help. Only removing the cause fixes it, so ask the organisers about flag toggling; otherwise escalate.

### 3.2 Payment unreachable (`paymentUnreachable`)
- **What happens:** checkout dials `badAddress:50051` instead of payment, so DNS fails and every charge fails at once.
- **First signal:** checkout client spans to PaymentService fail with gRPC Unavailable (name resolution), at 100%.
- **Strongest signal:** **payment's server-side `Charge` spans drop to zero** while the payment container is running and healthy. The error text names `badAddress:50051`, not `payment:50051`.
- **Knock-on:** checkout `PlaceOrder` 100% errors; frontend checkout 500.
- **Look-alikes:** payment container stopped (DNS also fails); network loss between checkout and payment.
- **Discriminator:**
  - Container stopped: `docker inspect` shows exited, and **all** payment telemetry stops, including runtime metrics.
  - Unreachable: payment is up and its process metrics continue; only Charge traffic vanishes; errors are immediate (no timeout latency).
  - Network loss: payment receives *some* calls, and there's latency or timeouts.
- **Fix:** ⚠ restarting payment doesn't help, because the bad address is on the checkout side. Flag toggle or escalate.

### 3.3 Cart fails intermittently (`cartFailure`, 10–100%)
- **What happens:** **only `EmptyCart`** fails. For n% of calls, cart uses a bad cache client (`badhost:1234`) and gets a connection error.
- **First signal:** cart server span errors on `EmptyCart` only.
- **Strongest signal:** errors confined to `span_name` EmptyCart; the exception mentions a connection failure to `badhost:1234`. GetCart and AddItem stay healthy.
- **Knock-on:** small. **Checkout ignores the EmptyCart error** (`_ = cs.emptyUserCart(...)`), so orders still succeed. The visible symptom is carts not clearing after checkout.
- **Look-alike:** Valkey (cache) down.
- **Discriminator:** with Valkey down, *all* cart operations fail, the redis receiver metrics stop, and the errors name `valkey-cart:6379`.
- **Fix:** ⚠ a restart won't help. Flag toggle or escalate. Low user impact, so escalating with findings is a fine outcome.

### 3.4 Cart readiness probe (`failedReadinessProbe`)
- **What happens:** cart's health check reports Unhealthy ("connection failed").
- **Expected under Docker Compose:** **probably no visible effect**, since readiness probes matter in Kubernetes. The gRPC health endpoint reports NOT_SERVING. **Verify live.**
- **Fix:** probably skip unless the organisers use Kubernetes.

### 3.5 Product catalog fails for one product (`productCatalogFailure`)
- **What happens:** `GetProduct` for product **OLJCESPC7Z** (the National Park Foundation Explorascope) fails with Internal.
- **First signal:** product-catalog error ratio rises, but only on `GetProduct`.
- **Strongest signal:** **every error span has the same `demo.product.id`**, and other products work. The status text contains the flag phrase, which must be redacted (section 2).
- **Knock-on:** frontend product page 500 for that product; checkout `prepareOrderItemsAndShippingQuoteFromCart` fails when the cart contains it; recommendations may fail when they include it.
- **Look-alikes:** database down (all products fail); lock contention (slow, not errors).
- **Discriminator:** errors concentrated on **one attribute value**. The "error concentration by attribute" check is worth a generic rule: when more than 90% of errors share one value of `demo.product.id`, `user.id` or similar, it's a data-specific fault, not a service outage.
- **Fix:** ⚠ a restart won't help. Flag toggle or escalate.

### 3.6 Database lock contention (`productCatalogLockContention`)
- **What happens:** every 10 s, product-catalog takes an `ACCESS EXCLUSIVE` lock on `catalog.products` for 30 s, so reads block. That's near-continuous blocking with short gaps.
- **First signal:** `db_client_operation_duration` and product-catalog latency jump, mostly as slowness and timeouts rather than errors.
- **Strongest signal:** a sawtooth pattern of ~30 s stalls; Postgres backends waiting; product-catalog logs "lock contention scenario active: holding ACCESS EXCLUSIVE lock on catalog.products". That's honest application telemetry, not flag text, but keep it under review.
- **Knock-on:** frontend homepage and product pages slow; recommendation slow (it calls ListProducts); checkout slow.
- **Look-alikes:** DB down (connection errors, not latency); CPU starvation of product-catalog (latency without DB waits); network latency (client vs. server gap).
- **Fix:** ⚠ restarting product-catalog might *briefly* release the lock, but the loop restarts it. Flag toggle or escalate.

### 3.7 Recommendation memory leak from a runaway cache (`recommendationCacheFailure`)
- **What happens:** each cache miss (50%) appends the whole catalog plus a quarter of the cache to itself, so the cache grows about 1.25× per miss.
- **First signal:** recommendation JVM heap and container memory climb steadily; `ListRecommendations` latency creeps up.
- **Strongest signal:** monotonic memory growth toward the **500M limit**, then an **OOM kill and restart** (`OOMKilled=true`, `RestartCount` up), and the cycle repeats. GC activity rises.
- **Side signals:** span attribute `demo.recommendation.cache_hit` and log lines "get_product_list: cache miss/hit" appear (these seem to be logged only in the leaky path; verify). Product-catalog `ListProducts` calls rise, since each miss refetches the catalog. The attribute `demo.feature_flag.recommendation_cache` and `feature_flag` events **must be redacted**.
- **Look-alike:** memory starvation, where the limit is lowered by the organisers.
- **Discriminator:** leak = growth from a normal baseline over minutes with the limit unchanged. Starvation = `HostConfig.Memory` lowered and memory pinned at the new limit soon after start, with no growth trend.
- **Fix:** a **restart works temporarily** (memory resets, then grows again). Treat it as Tier 1 *mitigation*, and say in the report that it will recur unless the cause is turned off.

### 3.8 Ad service high CPU (`adHighCpu`)
- **What happens:** background busy-loop threads spin in the ad service.
- **First signal:** ad container CPU and `process_cpu_seconds_total` jump.
- **Strongest signal:** high **absolute** CPU usage (cores used) with no change in the CPU limit; `GetAds` latency up. Log "High CPU-Load problempattern enabled".
- **Knock-on:** frontend pages render slower (ads are fetched per page). Other services sharing the laptop's CPU may slow down too, which is misleading.
- **Look-alike:** CPU starvation, where `docker update --cpus` lowers the quota.
- **Discriminator:** high CPU = usage goes **up**. Starvation = usage stays **low and flat at the cap**, with `HostConfig.NanoCpus` lowered and latency up.
- **Fix:** a restart probably clears it until the next request re-triggers it (verify). Otherwise flag toggle or escalate.

### 3.9 Ad service heavy garbage collection (`adManualGc`)
- **What happens:** a full GC is forced on ad requests.
- **First signal:** GC duration and pause metrics spike; `GetAds` latency spikes.
- **Strongest signal:** GC time high while the heap isn't near its limit; logs "Triggering a manual garbage collection…" and "The artificially triggered GCs took: N ms". The WARN line contains the flag name and **must be redacted**.
- **Look-alike:** a memory leak (GC rises *because* the heap is full).
- **Discriminator:** GC is high **with low heap usage**.
- **Fix:** ⚠ flag toggle or escalate.

### 3.10 Ad service failures (`adFailure`)
- **What happens:** 1 in 10 `GetAds` calls throws UNAVAILABLE.
- **Signals:** ad error ratio about 10% on GetAds; span event "Error" with exception message; log "GetAds Failed with status UNAVAILABLE…".
- **Knock-on:** minimal, because the frontend tolerates missing ads. A good test that the agent doesn't overreact. **Escalating with a low-severity report is reasonable.**
- **Fix:** ⚠ flag toggle or escalate.

### 3.11 Email memory leak (`emailMemoryLeak`, 1×–10000×)
- **What happens:** every confirmation email is kept in memory, padded by the multiplier.
- **First signal:** email container memory grows **in step with checkouts**.
- **Strongest signal:** growth toward the **100M limit**, then OOM kill and restart.
- **Knock-on:** while email is restarting, checkout logs WARN "failed to send order confirmation" but **orders still succeed**, because checkout ignores email errors.
- **Discriminator vs. recommendation leak:** the service, plus correlation with the checkout rate.
- **Fix:** a restart works temporarily. Tier 1 mitigation; the report should say it will recur.

### 3.12 Homepage traffic flood (`loadGeneratorFloodHomepage`)
- **What happens:** the load generator hammers `GET /`.
- **First signal:** **frontend request rate** jumps well above baseline.
- **Strongest signal:** request rate up many times over on the frontend and frontend-proxy; load-generator spans `user_flood_home` with attribute `demo.request.flood.count`; downstream rates (product-catalog, currency, ad, recommendation) rise with it.
- **Knock-on:** latency up everywhere and CPU up on the frontend. **Many services look sick at once**, which is a classic misattribution trap.
- **Discriminator:** request rate **rises first**, and latency follows. Errors and latency elsewhere scale with load.
- **Fix:** **throttle the load generator** (an Action API action). Tier 1.

### 3.13 Slow international shipping (`intlShippingSlowdown`, 5 s or 10 s)
- **What happens:** shipping sleeps 5–10 s for non-US addresses.
- **Signal:** shipping latency is **bimodal**: normal for US, +5–10 s for international. Checkout latency is up for those orders.
- **Leak:** the shipping logs contain the flag key and name, so **redact** them.
- **Discriminator vs. network latency:** server-side shipping spans are slow too (network latency would show client ≫ server). Only some requests are slow, split by country.
- **Fix:** ⚠ flag toggle or escalate.

### 3.14 Slow image loading (`imageSlowLoad`)
- **What happens:** the browser adds the header `x-envoy-fault-delay-request` to image requests, and Envoy delays them.
- **Expected:** **probably invisible under Locust load**, because only real browsers render product images. Verify live by opening the shop in a browser.
- **Low priority.**

### Not relevant in minimal mode
- `kafkaQueueProblems` and `kafkaConsumerDead` need Kafka and fraud-detection (full mode only).
- `aiSlowResponse` and `aiRunawayAgent` need the demo's own AI agent service (`compose.agent.yaml`).
- `emitRawPii` and `cartMetricCardinalityOverflow` aren't failure scenarios.

---

## 4. Infrastructure faults (injected by the organisers)

These aren't in the source code, so here's what to expect plus a manual experiment to confirm each one live. **Reset everything afterwards** (`docker start …`, `docker update` back to the original values, or `make start-minimal`).

| Fault | Expected signals | Discriminator | Try it |
|---|---|---|---|
| **Container stopped** (for example payment) | All its telemetry stops (spans, runtime and container metrics); callers fail with Unavailable or name-resolution errors naming `payment:50051` | `docker inspect` shows `exited`; vs. `paymentUnreachable`, where payment is still running | `docker stop payment` |
| **Crash or restart loop** | `RestartCount` rising, uptime resetting, gaps in metrics, bursts of caller errors | Restarts without memory growth = crash; with growth to the limit = leak or OOM | `docker kill payment` a few times (auto-restarts) |
| **Cache unavailable** (valkey-cart) | Cart GetCart, AddItem and EmptyCart **all** fail with errors naming `valkey-cart:6379`; redis receiver metrics stop; checkout fails when reading the cart | vs. `cartFailure`, which affects EmptyCart only and names `badhost:1234` | `docker stop valkey-cart` |
| **Database unavailable** (astronomy-db) | product-catalog errors on **every** product (connection refused); homepage 500; postgresql metrics stop | vs. one-product failure, and vs. lock contention (slow, not erroring) | `docker stop astronomy-db` |
| **Network latency between two services** | **Client-side** rpc duration up while **server-side** duration is normal for that hop | client p95 − server p95 = injected delay | `docker run --rm --net container:shipping --cap-add NET_ADMIN nicolaka/netshoot tc qdisc add dev eth0 root netem delay 500ms` ⚠ downloads the netshoot image; affects *all* of shipping's traffic |
| **Packet loss between two services** | Intermittent Unavailable or DeadlineExceeded on that hop, retries, latency spikes; both sides healthy | Errors only on calls *across that edge* | same as above with `netem loss 30%` |
| **CPU starvation** | Latency up; container CPU **low and flat at the cap**; nothing else changed | `HostConfig.NanoCpus` lowered; vs. `adHighCpu`, where usage goes up | `docker update --cpus 0.05 ad` |
| **Memory starvation** | Memory pinned at a new low limit, OOM kills and restarts soon after start | `HostConfig.Memory` lowered; no growth trend (vs. a leak) | `docker update --memory 60m --memory-swap 60m recommendation` |

---

## 5. Look-alikes: what the competing-hypotheses step has to separate

| Symptom seen | Competing hypotheses | Deciding evidence |
|---|---|---|
| Checkout errors on charging | payment logic failing · payment unreachable · payment container down · network loss checkout→payment | Do payment server spans exist? Are they erroring? Is the payment container running? Client vs. server latency? Which address is in the error? |
| Cart errors | cart logic (EmptyCart only) · Valkey down · cart starved | Which operations fail? Are redis metrics present? Which host is in the error? Container limits? |
| Product pages failing or slow | one-product failure · DB down · lock contention · catalog CPU-starved | Errors vs. latency? Concentrated on one product ID? DB metrics present? DB waits? NanoCpus? |
| A service's memory climbing | leak (recommendation or email) · memory starvation | Limit changed? Gradual growth from baseline? Correlated with checkouts? |
| A service's latency up, CPU odd | high-CPU bug · CPU starvation · GC storm · traffic flood | CPU going up or flat at the cap? GC time vs. heap? Request rate up first? |
| Everything slow at once | traffic flood · a shared dependency (DB, cache) slow · the laptop saturated | Frontend request rate first? One dependency's latency first? Host CPU? |
| Hop slow between A and B | network latency · B slow | Client p95 vs. server p95 for that hop |

---

## 6. Combinations to test (the hidden test includes them)
1. **paymentFailure + adHighCpu:** independent faults. Does the agent fix one, verify, then find the other?
2. **recommendationCacheFailure + productCatalogLockContention:** entangled, because recommendation calls the catalog. Does it untangle cause from symptom?
3. **Valkey down + paymentFailure:** both break checkout. Does it avoid blaming checkout and fix them in order?
4. **Homepage flood + adHighCpu:** both cause latency. Does it notice the request-rate surge separately from ad CPU?
5. **DB down + productCatalogFailure:** one masks the other. After restoring the DB, does it notice that one product still fails?

---

## 7. Live observation template (one per fault during the prep run)

| Field | Record |
|---|---|
| Fault and setting | e.g. paymentFailure 50% |
| Time started (clock) | |
| **First signal** (what, where, when) | |
| **Strongest signal** (what proves it) | |
| Error ratio and p95 for the failing service | |
| Knock-on services and their error/latency | |
| **Misleading signals** (what looked guilty but wasn't) | |
| Container state (status, RestartCount, OOMKilled, memory, CPU) | |
| Log lines worth matching (after redaction) | |
| Queries that revealed it (PromQL / Jaeger) | |
| Did any flag text appear in telemetry? Where? | |
| Time to detect with our planned thresholds | |
| Recovery after turning the fault off (how long?) | |
| Seen live? | ☐ |

---

## 8. Still to verify live
- [ ] Exact container metric names, and whether a CPU-throttling metric exists.
- [ ] JVM GC metric name for ad and recommendation.
- [ ] Actual metric export interval (how quickly do span metrics move after a fault starts?).
- [ ] Whether `feature_flag` span events also appear in services other than recommendation (ad, cart, checkout, product-catalog).
- [ ] Whether `failedReadinessProbe` and `imageSlowLoad` have any visible effect under Docker Compose.
- [ ] Whether a restart clears `adHighCpu`, and how fast the leaks come back after a restart.
- [ ] Baselines with no fault: normal error ratio, p95 and request rate per service, needed to set thresholds.
