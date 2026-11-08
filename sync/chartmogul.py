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


class ChartMogulTransientError(ChartMogulError):
    """Retries exhausted on 429/5xx/timeouts (as opposed to a 4xx we shouldn't retry)."""


def _get(path: str, params: dict | None = None, attempts: int = MAX_ATTEMPTS) -> dict:
    """GET with backoff on 429, 5xx and network errors. One flaky request should not
    throw away an hour-long sync."""
    if not API_KEY:
        raise ChartMogulError("CHARTMOGUL_API_KEY is not set")
    last = None
    for attempt in range(attempts):
        wait = min(2 ** attempt, 60)
        try:
            resp = _http.get(f"{BASE}{path}", params=params or {},
                             auth=(API_KEY, ""), timeout=75)  # CM itself gives up at ~60s
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
    raise ChartMogulTransientError(f"Gave up on {path} after {attempts} attempts ({last})")


def _paginate(path: str, list_key: str, params: dict | None = None, per_page: int = 100):
    """Yield entries across cursor-paginated responses, with adaptive page size.

    ChartMogul builds list pages synchronously and returns 500 when a page takes >60s.
    Recently created customers are slow (observed 2026-11-02: 40 customers created after
    2026-10-21 took 11s; a 100-row page from 2026-10-14 always 500'd). So on repeated
    failure we retry the *same cursor* with a quarter of the page size, down to 1, then
    grow back after successes.
    """
    params = dict(params or {})
    size, streak = per_page, 0
    while True:
        params["per_page"] = size
        try:
            data = _get(path, params, attempts=2 if size > 1 else MAX_ATTEMPTS)
        except ChartMogulTransientError:
            if size == 1:
                raise
            size, streak = max(1, size // 4), 0
            log.warning("ChartMogul %s page keeps failing; retrying with per_page=%d",
                        path, size)
            continue
        for entry in data.get(list_key) or data.get("entries") or []:
            yield entry
        if not data.get("has_more") or not data.get("cursor"):
            break
        params["cursor"] = data["cursor"]
        streak += 1
        if size < per_page and streak >= 5:  # each failed probe costs ~2 min; grow slowly
            size, streak = min(per_page, size * 2), 0


def iter_customers(statuses: list[str]):
    """Customers in the given statuses (filtered server-side), e.g. ["Active"]."""
    for status in statuses:
        yield from _paginate("/customers", "entries", {"status": status})


def iter_activities_since(start_iso: str):
    """Account-wide MRR activity feed (new_biz, expansion, contraction, churn,
    reactivation) since start_iso. One fast paginated stream; the nightly incremental
    refresh uses it to find which customers changed (~30 a day of ~15k)."""
    yield from _paginate("/activities", "entries", {"start-date": start_iso}, per_page=200)


def fetch_customer(customer_uuid: str) -> dict | None:
    """One customer record (same shape as /customers list entries); None if deleted."""
    try:
        return _get(f"/customers/{customer_uuid}")
    except ChartMogulError as exc:
        if str(exc).startswith("404"):
            return None
        raise


def iter_plans():
    """Every plan (product/SKU) across data sources."""
    yield from _paginate("/plans", "plans")


def fetch_subscriptions(customer_uuid: str) -> list[dict]:
    return list(_paginate(f"/customers/{customer_uuid}/subscriptions", "entries"))


def fetch_activities(customer_uuid: str) -> list[dict]:
    """MRR movements for one customer (new_biz, expansion, contraction, churn, ...)."""
    return list(_paginate(f"/customers/{customer_uuid}/activities", "entries"))


def fetch_customer_detail(customer_uuid: str) -> tuple[list[dict], list[dict]]:
    return fetch_subscriptions(customer_uuid), fetch_activities(customer_uuid)


def map_customer_details(customer_uuids, workers: int = WORKERS):
    """Yield (customer_uuid, subscriptions, activities) per customer, fetched concurrently.
    Results arrive in input order; HTTP happens in threads, DB writes stay with the caller."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for uuid, (subs, acts) in zip(customer_uuids,
                                      pool.map(fetch_customer_detail, customer_uuids)):
            yield uuid, subs, acts


PLANS_PER_METRICS_CALL = 40  # 200 plan UUIDs in one query string -> HTTP 414


def fetch_product_mrr(plan_uuids: list[str], start: str, end: str) -> dict[str, dict]:
    """Monthly MRR (+ movement breakdown) for a set of plans, keyed by ISO date.
    Plans are queried in batches and summed; MRR is additive across disjoint plans."""
    keys = ("mrr", "mrr-new-business", "mrr-expansion", "mrr-contraction", "mrr-churn",
            "mrr-reactivation")
    out: dict[str, dict] = {}
    for i in range(0, len(plan_uuids), PLANS_PER_METRICS_CALL):
        batch = plan_uuids[i:i + PLANS_PER_METRICS_CALL]
        data = _get("/metrics/mrr", {"start-date": start, "end-date": end,
                                     "interval": "month", "plans": ",".join(batch)})
        for e in data.get("entries", []):
            row = out.setdefault(e["date"], dict.fromkeys(keys, 0))
            for k in keys:
                row[k] += e.get(k) or 0
    return out


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
