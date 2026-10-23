"""Place real checkouts through the shop's frontend, one every few seconds, and print what a
customer would see. Demo traffic for the payment-failure demo: the load generator only checks
out about once every 30 s on a laptop, too rarely to show a payment failure quickly.

    python scripts/checkouts.py                 # one every 5 s until Ctrl-C
    python scripts/checkouts.py --count 3       # three, then stop

This is customer traffic, not part of the agent: the agent never calls it. Keep it running for
the whole demo (baseline included), so the checkout rate does not drop when it stops.
"""
from __future__ import annotations

import argparse
import itertools
import sys
import time
import uuid
from datetime import datetime

import httpx

# demo catalog product IDs, as in the demo's own tests and load generator
PRODUCTS = ["0PUK6V6EV0", "1YMWWN1N4O", "2ZYFJ3GM2N", "66VCHSJNUP", "6E92ZMYYFZ",
            "9SIQT8TOJO", "L9ECAV7KIM", "LS4PSXUNUM", "HQTGWGPNH4"]
PERSON = {
    "email": "demo-customer@example.com",
    "address": {"streetAddress": "1600 Amphitheatre Parkway", "zipCode": "94043",
                "city": "Mountain View", "state": "CA", "country": "United States"},
    "userCurrency": "USD",
    "creditCard": {"creditCardNumber": "4432-8015-6152-0454", "creditCardExpirationMonth": 1,
                   "creditCardExpirationYear": 2039, "creditCardCvv": 672},
}


def checkout(http: httpx.Client, base: str, product: str) -> tuple[bool, str]:
    user = str(uuid.uuid4())
    r = http.post(f"{base}/api/cart", json={"item": {"productId": product, "quantity": 1}, "userId": user})
    if r.status_code >= 400:
        return False, f"add to cart: HTTP {r.status_code} {r.text.strip()[:160]}"
    r = http.post(f"{base}/api/checkout", json=dict(PERSON, userId=user))
    if r.status_code >= 400:
        return False, f"HTTP {r.status_code} {r.text.strip()[:200]}"
    try:
        order = r.json().get("orderId", "")
    except ValueError:
        order = ""
    return True, f"order {order}".strip()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default="http://localhost:8080")
    p.add_argument("--every", type=float, default=5.0, help="seconds between checkouts")
    p.add_argument("--count", type=int, default=0, help="stop after this many (0 = until Ctrl-C)")
    args = p.parse_args(argv)
    ok = failed = 0
    with httpx.Client(timeout=20) as http:
        try:
            for n in itertools.count(1):
                if args.count and n > args.count:
                    break
                try:
                    good, detail = checkout(http, args.base, PRODUCTS[n % len(PRODUCTS)])
                except httpx.HTTPError as e:
                    good, detail = False, f"{type(e).__name__}: {e}"
                ok, failed = ok + good, failed + (not good)
                print(f"{datetime.now():%H:%M:%S} checkout {n}: {'OK    ' if good else 'FAILED'} {detail}", flush=True)
                time.sleep(args.every)
        except KeyboardInterrupt:
            pass
    print(f"{ok} ok, {failed} failed")
    return 0 if ok or not failed else 1


if __name__ == "__main__":
    sys.exit(main())
