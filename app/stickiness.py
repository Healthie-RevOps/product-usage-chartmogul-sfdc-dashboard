"""Stickiness by plan: who stays, how revenue holds up, and who's paying but not using.

All from data we already sync:
- Retention: ChartMogul subscription start/end dates say whether a customer was paying on a
  date and which plan (app/segments.py) they were on then. A customer is in a plan's 12-month
  cohort if they paid for that plan 12 months ago; retained = still paying today (any plan).
- Revenue retention: each ChartMogul activity carries the customer's total MRR after it, so
  MRR on a past date = MRR after their last activity before it.
  NRR = cohort MRR today / cohort MRR then; GRR caps each customer at their MRR then.
- Usage: provider / client counts per Healthie org (Metabase), today only, so usage is a
  current-health signal, not a historical one.
Partner revenue buckets ("Non EHR MRR: ...") are excluded throughout.
"""
from __future__ import annotations

import threading
from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from statistics import median

from app import data, segments
from app.db import Activity

MIN_TREND_COHORT = 20      # smaller 12-month cohorts are noise; left off the trend chart
INACTIVE_DAYS = 30          # no provider login in this long (or ever) = not using it


def _d(dt):
    return dt.date() if dt else None


class _Cust:
    __slots__ = ("uuid", "name", "account_id", "mrr_now", "subs", "first_start", "act_dates",
                 "act_mrr", "force_plan", "fallback_plan")


def _load(session, snap) -> list[_Cust]:
    acts: dict[str, list] = defaultdict(list)
    for uuid, when, mrr in (session.query(Activity.customer_uuid, Activity.date, Activity.mrr_cents)
                            .order_by(Activity.date)):
        if when is not None:
            acts[uuid].append((when.date(), mrr or 0))
    out = []
    for uuid, c in snap.customers.items():
        if uuid in snap.partners:
            continue
        subs = [(_d(s.start), None if s.current else (_d(s.end) or _d(s.start)), s.product)
                for s in snap.subs_by_customer.get(uuid, []) if s.start]
        if not subs:
            continue
        acct = snap.accounts.get(c["account_id"]) if c["account_id"] else None
        x = _Cust()
        x.uuid, x.name, x.account_id, x.mrr_now = uuid, c["name"], c["account_id"], c["mrr"]
        x.subs = subs
        x.first_start = min(s[0] for s in subs)
        a = acts.get(uuid, [])
        x.act_dates, x.act_mrr = [d for d, _ in a], [m for _, m in a]
        sfdc_plan = segments.from_sfdc(acct.get("sfdc_plan"), acct.get("has_api")) if acct else None
        x.force_plan = segments.ENTERPRISE if sfdc_plan == segments.ENTERPRISE else None
        x.fallback_plan = sfdc_plan
        out.append(x)
    return out


def _active(c: _Cust, d: date) -> bool:
    return any(st <= d and (en is None or en > d) for st, en, _ in c.subs)


def _plan(c: _Cust, d: date) -> str | None:
    if c.force_plan:
        return c.force_plan
    prods = {p for st, en, p in c.subs if st <= d and (en is None or en > d)}
    return segments.from_products(prods) or c.fallback_plan


def _mrr_at(c: _Cust, d: date) -> int:
    i = bisect_right(c.act_dates, d)
    return c.act_mrr[i - 1] if i else 0


def _month_ends(n: int, today: date) -> list[date]:
    out, d = [], today.replace(day=1) - timedelta(days=1)
    for _ in range(n):
        out.append(d)
        d = d.replace(day=1) - timedelta(days=1)
    return list(reversed(out)) + [today]


# ------------------------------------------------------------------ cached build

_lock = threading.Lock()
_cache: dict = {"key": None, "val": None}


def build(session, snap) -> dict:
    if _cache["key"] == snap.run_id and _cache["val"] is not None and snap.run_id is not None:
        return _cache["val"]
    with _lock:
        if not (_cache["key"] == snap.run_id and _cache["val"] is not None):
            _cache["val"] = _build(session, snap)
            _cache["key"] = snap.run_id
    return _cache["val"]


def invalidate():
    _cache["key"] = _cache["val"] = None


def _usage(snap, account_id):
    a = snap.accounts.get(account_id) if account_id else None
    if not a or a.get("hl_providers") is None:
        return None
    return a


def _build(session, snap) -> dict:
    today = date.today()
    year_ago, q_ago = today - timedelta(days=365), today - timedelta(days=90)
    custs = _load(session, snap)
    plans = segments.PLANS
    per = {p: {"active": [], "cohort12": [], "cohort90": []} for p in plans + [segments.ALL]}
    for c in custs:
        if _active(c, today):
            p = _plan(c, today)
            if p:
                per[p]["active"].append(c)
                per[segments.ALL]["active"].append(c)
        if _active(c, year_ago):
            p = _plan(c, year_ago)
            if p:
                per[p]["cohort12"].append(c)
                per[segments.ALL]["cohort12"].append(c)
        if _active(c, q_ago):
            p = _plan(c, q_ago)
            if p:
                per[p]["cohort90"].append(c)
                per[segments.ALL]["cohort90"].append(c)

    summary = []
    for p in plans + [segments.ALL]:
        g = per[p]
        active, coh, coh90 = g["active"], g["cohort12"], g["cohort90"]
        retained = [c for c in coh if _active(c, today)]
        mrr_then = sum(_mrr_at(c, year_ago) for c in coh)
        mrr_now_coh = sum(c.mrr_now for c in retained)
        grr_num = sum(min(c.mrr_now, _mrr_at(c, year_ago)) for c in retained)
        tenures = [(today - c.first_start).days / 30.44 for c in active]
        with_usage = [u for u in (_usage(snap, c.account_id) for c in active) if u]
        using = [u for u in with_usage if _recent_login(u, today)]
        summary.append({
            "plan": p,
            "active": len(active),
            "mrr": sum(c.mrr_now for c in active),
            "new_12m": sum(1 for c in active if c.first_start > year_ago),
            "median_tenure_months": round(median(tenures), 1) if tenures else None,
            "cohort_12m": len(coh),
            "logo_retention_12m": len(retained) / len(coh) if coh else None,
            "churned_12m": len(coh) - len(retained),
            "churned_90d": sum(1 for c in coh90 if not _active(c, today)),
            "nrr_12m": mrr_now_coh / mrr_then if mrr_then else None,
            "grr_12m": grr_num / mrr_then if mrr_then else None,
            "usage_coverage": len(with_usage) / len(active) if active else None,
            "using_share": len(using) / len(with_usage) if with_usage else None,
            "median_active_providers": (median(u.get("hl_active_providers") or 0 for u in with_usage)
                                        if with_usage else None),
            "paying_inactive": sum(1 for c in active if _is_inactive(snap, c, today)),
        })

    # 12-month logo retention measured at each of the last 12 month ends
    points = _month_ends(12, today)
    trend = {p: [] for p in plans}
    sizes = {p: [] for p in plans}
    for m in points:
        then = m - timedelta(days=365)
        coh: dict[str, list] = defaultdict(list)
        for c in custs:
            if _active(c, then):
                p = _plan(c, then)
                if p:
                    coh[p].append(c)
        for p in plans:
            members = coh.get(p, [])
            kept = sum(1 for c in members if _active(c, m))
            trend[p].append(kept / len(members) if members else None)
            sizes[p].append(len(members))

    return {"summary": summary,
            "trend": {"dates": [d.isoformat() for d in points], "series": trend, "cohorts": sizes,
                      "min_cohort": MIN_TREND_COHORT},
            "custs": custs, "today": today, "q_ago": q_ago, "per": per}


def _recent_login(u: dict, today: date) -> bool:
    """A provider at the org logged in within INACTIVE_DAYS. ("Active providers" alone
    isn't usage: it means not deactivated, and nearly every org has one.)"""
    last = u.get("hl_last_activity")
    return bool(last) and date.fromisoformat(last) >= today - timedelta(days=INACTIVE_DAYS)


def _is_inactive(snap, c: _Cust, today: date) -> bool:
    """Paying, Healthie usage data available, and no provider login in INACTIVE_DAYS
    (including never)."""
    if c.mrr_now <= 0:
        return False
    u = _usage(snap, c.account_id)
    return bool(u) and not _recent_login(u, today)


# ------------------------------------------------------------------ lists

def at_risk(snap, built: dict, plan: str) -> list[dict]:
    today = built["today"]
    out = []
    for c in built["per"].get(plan or segments.ALL, built["per"][segments.ALL])["active"]:
        if not _is_inactive(snap, c, today):
            continue
        u = _usage(snap, c.account_id)
        acct = snap.accounts.get(c.account_id) or {}
        out.append({"uuid": c.uuid, "name": c.name, "plan": _plan(c, today), "mrr": c.mrr_now,
                    "active_providers": u.get("hl_active_providers"), "providers": u.get("hl_providers"),
                    "last_activity": u.get("hl_last_activity"), "active_clients": u.get("hl_active_clients"),
                    "owner": acct.get("owner"), "account_id": c.account_id,
                    "tenure_months": round((today - c.first_start).days / 30.44, 1)})
    out.sort(key=lambda r: -r["mrr"])
    return out


def churned(built: dict, plan: str) -> list[dict]:
    today, q_ago = built["today"], built["q_ago"]
    out = []
    for c in built["per"].get(plan or segments.ALL, built["per"][segments.ALL])["cohort90"]:
        if _active(c, today):
            continue
        ended = max((en for _, en, _ in c.subs if en), default=None)
        out.append({"uuid": c.uuid, "name": c.name, "plan": _plan(c, q_ago),
                    "mrr_lost": _mrr_at(c, q_ago), "ended": ended.isoformat() if ended else None,
                    "account_id": c.account_id,
                    "tenure_months": round(((ended or today) - c.first_start).days / 30.44, 1)})
    out.sort(key=lambda r: -r["mrr_lost"])
    return out
