# Demo: the payment service fails when charging

What the audience sees:
1. The shop works; checkouts succeed.
2. `paymentFailure` is switched on. Customers' checkouts start failing in the shop and in the
   checkout terminal.
3. The agent notices within a minute or two. It finds that the errors start in payment's
   `Charge` call, not in checkout, and that every failing charge is a gold-tier customer. It
   explains why no allowed fix applies, and escalates with the evidence.
4. A human switches the flag off, and the agent confirms the recovery.

Three terminals, each in `~/Desktop/team-15-bosch` with `source .venv/bin/activate`.

## Before the audience arrives (about 5 minutes)

All flags off at http://localhost:8080/feature, and payment running (`docker start payment`).

Terminal 2, steady customer traffic (keep it running the whole time):

```sh
python scripts/checkouts.py
```

Every line should say `OK`. If any says `FAILED` with every flag off, fix that first.

After 3 minutes of all OK, terminal 1:

```sh
python -m agent.eyes baseline --window-min 3
```

Every service should show 0.0% errors, with no warning at the end.

## The demo

Terminal 1, start the agent:

```sh
python -m agent.run watch
```

Browser: at http://localhost:8080/feature set `paymentFailure` to 100% (or 50%).

- Terminal 2 turns to `FAILED HTTP 500 ... Payment request failed. Invalid token.`, which is
  the customer's view. You can also show it in the shop: add an item, check out, see the error.
- Terminal 1, within 1 to 2 minutes, prints the alert, then the report:
  - root cause **payment / service_errors** from the rules, confidence 0.80,
  - evidence: payment's error ratio against baseline; in traces, payment's `Charge` is the deepest
    failing span while checkout and frontend only pass the error on; every error has
    `demo.user_context.loyalty_level = gold`,
  - outcome **escalated**: the allowed actions are restarts and throttling, and a restart does not
    stop a payment bug, so a human gets the diagnosis instead of a wrong fix.

Then switch `paymentFailure` back to off, and show the agent confirm the recovery
(terminal 3, takes up to 3 minutes):

```sh
python -m agent.eyes verify payment error_ratio
```

It prints `"recovered": true`, and terminal 2 goes back to `OK`.

## If something goes wrong

- Terminal 1 alerts on `p95_ms` before you flip the flag: the laptop is overloaded. Close other
  apps, keep OpenSearch and Grafana stopped, retake the baseline.
- No alert after 3 minutes: check terminal 2 really shows failures, then run
  `python -m agent.eyes tool service_health_overview` and look at payment's error_ratio.
- The report's reason and evidence say why it decided what it decided; reports are saved in
  `runs/<time>/`.
