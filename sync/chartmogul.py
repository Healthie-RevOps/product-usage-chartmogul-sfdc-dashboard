"""ChartMogul API client.

Auth: API key as HTTP Basic username, blank password.
Docs: https://dev.chartmogul.com/reference
Pagination: cursor-based (`cursor` + `has_more`).

There is no "subscriptions by plan" endpoint, so the sync lists customers filtered
server-side by status, then fetches each customer's subscriptions in parallel.
"""
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("sync.chartmogul")

BASE = "https://api.chartmogul.com/v1"
API_KEY = os.environ.get("CHARTMOGUL_API_KEY", "")

# ChartMogul allows ~40 req/s per account. WORKERS x (latency + delay) stays well under it.
REQUEST_DELAY_S = float(os.environ.get("CM_REQUEST_DELAY_S", "0.1"))
WORKERS = int(os.environ.get("CM_WORKERS", "10"))
MAX_ATTEMPTS = 9  # backoff 1,2,4..60s: rides out ~5 min of ChartMogul 500s/timeouts

_http = requests.Session()


class ChartMogulError(RuntimeError):
    pass


def _get(path: str, params: dict | None = None) -> dict:
    """GET with backoff on 429, 5xx and network errors. One flaky request should not
    throw away an hour-long sync."""
    if not API_KEY:
        raise ChartMogulError("CHARTMOGUL_API_KEY is not set")
    last = None
    for attempt in range(MAX_ATTEMPTS):
        wait = min(2 ** attempt, 60)
        try:
            resp = _http.get(f"{BASE}{path}", params=params or {},
                             auth=(API_KEY, ""), timeout=60)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = f"network error: {exc}"
            log.warning("ChartMogul %s on %s, retrying in %ss", type(exc).__name__, path, wait)
            time.sleep(wait)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            last = f"{resp.status_code}: {resp.text[:200]}"
            log.warning("ChartMogul %s on %s, retrying in %ss", resp.status_code, path, wait)
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            raise ChartMogulError(f"{resp.status_code} on {path}: {resp.text[:300]}")
        time.sleep(REQUEST_DELAY_S)
        return resp.json()
    raise ChartMogulError(f"Gave up on {path} after {MAX_ATTEMPTS} attempts ({last})")


def _paginate(path: str, list_key: str, params: dict | None = None):
    """Yield entries across cursor-paginated responses."""
    params = dict(params or {})
    params.setdefault("per_page", 100)  # 200 times out more often under load
    while True:
        data = _get(path, params)
        for entry in data.get(list_key) or data.get("entries") or []:
            yield entry
        if not data.get("has_more") or not data.get("cursor"):
            break
        params["cursor"] = data["cursor"]


def iter_customers(statuses: list[str]):
    """Customers in the given statuses (filtered server-side), e.g. ["Active"]."""
    for status in statuses:
        yield from _paginate("/customers", "entries", {"status": status})


def iter_plans():
    """Every plan (product/SKU) across data sources."""
    yield from _paginate("/plans", "plans")


def fetch_subscriptions(customer_uuid: str) -> list[dict]:
    return list(_paginate(f"/customers/{customer_uuid}/subscriptions", "entries"))


def map_subscriptions(customer_uuids, workers: int = WORKERS):
    """Yield (customer_uuid, subscriptions) for each customer, fetched concurrently.
    Results arrive in input order; HTTP happens in threads, DB writes stay with the caller."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        yield from zip(customer_uuids, pool.map(fetch_subscriptions, customer_uuids))


def product_family(plan_name: str | None) -> str | None:
    """Collapse per-customer plan variants into a product.
    'Standard Seats - Weis Markets Inc.' -> 'Standard Seats' (877 plans -> ~258 products)."""
    if not plan_name:
        return None
    return plan_name.split(" - ")[0].strip() or plan_name


def extract_stripe_id(customer: dict) -> str | None:
    """ChartMogul exposes the source-system id as external_id(s). For a Stripe
    data source this is the cus_... id."""
    ext = customer.get("external_id")
    if ext and str(ext).startswith("cus_"):
        return ext
    for candidate in customer.get("external_ids") or []:
        if str(candidate).startswith("cus_"):
            return candidate
    return ext  # may still be a usable id even if not cus_-prefixed


def extract_org_id(customer: dict) -> str | None:
    """Healthie Org ID if it has been pushed as a CM custom attribute."""
    custom = (customer.get("attributes") or {}).get("custom") or {}
    for key in ("healthie_org_id", "Healthie Org ID", "org_id", "Healthie_Org_ID"):
        if custom.get(key):
            return str(custom[key])
    return None
