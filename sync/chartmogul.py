"""ChartMogul API client.

Auth: API key as HTTP Basic username, blank password.
Docs: https://dev.chartmogul.com/reference
Pagination: cursor-based (`cursor` + `has_more`).
"""
import logging
import os
import time

import requests

log = logging.getLogger("sync.chartmogul")

BASE = "https://api.chartmogul.com/v1"
API_KEY = os.environ.get("CHARTMOGUL_API_KEY", "")

# ChartMogul allows ~40 req/s but there is no reason to hammer it from a daily cron.
REQUEST_DELAY_S = float(os.environ.get("CM_REQUEST_DELAY_S", "0.15"))


class ChartMogulError(RuntimeError):
    pass


def _get(path: str, params: dict | None = None) -> dict:
    if not API_KEY:
        raise ChartMogulError("CHARTMOGUL_API_KEY is not set")
    for attempt in range(4):
        resp = requests.get(f"{BASE}{path}", params=params or {},
                            auth=(API_KEY, ""), timeout=60)
        if resp.status_code == 429:
            wait = 2 ** attempt
            log.warning("429 from ChartMogul, backing off %ss", wait)
            time.sleep(wait)
            continue
        if resp.status_code >= 400:
            raise ChartMogulError(f"{resp.status_code} on {path}: {resp.text[:300]}")
        time.sleep(REQUEST_DELAY_S)
        return resp.json()
    raise ChartMogulError(f"Rate-limited repeatedly on {path}")


def _paginate(path: str, list_key: str, params: dict | None = None):
    """Yield entries across cursor-paginated responses."""
    params = dict(params or {})
    params.setdefault("per_page", 200)
    cursor = None
    while True:
        if cursor:
            params["cursor"] = cursor
        data = _get(path, params)
        for entry in data.get(list_key) or data.get("entries") or []:
            yield entry
        if not data.get("has_more"):
            break
        cursor = data.get("cursor")
        if not cursor:
            break


def iter_customers():
    """All ChartMogul customers with attributes."""
    yield from _paginate("/customers", "entries")


def iter_subscriptions(customer_uuid: str):
    """Current subscriptions for one customer (plan, MRR, quantity, cycle)."""
    yield from _paginate(f"/customers/{customer_uuid}/subscriptions", "entries")


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
