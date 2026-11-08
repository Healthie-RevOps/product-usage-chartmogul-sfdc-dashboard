"""Data-quality worklist: specific Salesforce fixes, each with an owner and the view it
improves. Every rule is cheap to fix in SFDC and directly sharpens the Enterprise and
Win / loss pages, which can only be as good as these fields.

Deal rules (owner = opportunity owner):
  missing_competitor        closed Initial Sales deal, competitor blank or "Other / Unknown"
                            in every field ("None (Healthie User)" counts as an answer)
  missing_loss_reason       lost deal (not an add-on), no Loss_Reason__c
  missing_addon_reason      lost add-on deal, no Add_On_Close_Lost_Reason__c
  missing_expansion_product Expansion Deal with no Expansion_Product__c (suggests one
                            from the deal name when it names an add-on)
  missing_acv               won Initial Sales / Expansion deal with no Year_1_ACV__c
  stale_open                open deal whose close date has passed

Account rule (owner = CSM):
  flag_mismatch             Enterprise account where a Stripe_Has_* flag disagrees with
                            live ChartMogul billing for that add-on
"""
from __future__ import annotations

import threading
from collections import Counter, defaultdict
from datetime import date

from app import data, deals, enterprise
from app.db import SfdcOpportunity

INITIAL = "Initial Sales Opportunity"
EXPANSION = "Expansion Deal"

RULES = [
    {"key": "missing_competitor", "label": "No competitor", "affects": "Win / loss",
     "fix": "Set Competitor (use “None (Healthie User)” only if truly none)"},
    {"key": "missing_loss_reason", "label": "No loss reason", "affects": "Win / loss",
     "fix": "Set Loss Reason"},
    {"key": "missing_addon_reason", "label": "No add-on loss reason", "affects": "Enterprise",
     "fix": "Set Add-On Close Lost Reason"},
    {"key": "missing_expansion_product", "label": "No Expansion Product", "affects": "Enterprise",
     "fix": "Set Expansion Product"},
    {"key": "missing_acv", "label": "Won, no Year 1 ACV", "affects": "Win / loss",
     "fix": "Set Year 1 ACV"},
    {"key": "stale_open", "label": "Close date passed", "affects": "Pipeline",
     "fix": "Update the close date or close the deal"},
    {"key": "flag_mismatch", "label": "Add-on flag ≠ billing", "affects": "Enterprise",
     "fix": "Check the Stripe_Has_* flag against Stripe"},
]
RULE_KEYS = [r["key"] for r in RULES]
DEAL_RULES = RULE_KEYS[:-1]
_ADDON_LABEL = {a["key"]: a["label"] for a in enterprise.ADDONS}


def deal_issues(o, today: date) -> tuple[list[str], set[str], dict]:
    """(issues, rules this deal is eligible for, suggestions) for one opportunity."""
    issues, eligible, hints = [], set(), {}
    lost = o.is_closed and not o.is_won
    is_addon = o.record_type == EXPANSION and o.opp_type == "Add-on"
    if o.record_type == INITIAL and o.is_closed:
        eligible.add("missing_competitor")
        if not deals.competitor_answered(o):
            issues.append("missing_competitor")
    if lost and not is_addon:
        eligible.add("missing_loss_reason")
        if not (o.loss_reason or "").strip():
            issues.append("missing_loss_reason")
    if lost and is_addon:
        eligible.add("missing_addon_reason")
        if not (o.addon_loss_reason or "").strip():
            issues.append("missing_addon_reason")
    if o.record_type == EXPANSION:
        eligible.add("missing_expansion_product")
        if not (o.expansion_product or "").strip():
            issues.append("missing_expansion_product")
            guess = sorted(enterprise.detect_addons(o))
            if guess:
                hints["expansion_product"] = ", ".join(_ADDON_LABEL[k] for k in guess)
    if o.is_won and o.record_type in (INITIAL, EXPANSION):
        eligible.add("missing_acv")
        if o.year1_acv is None:
            issues.append("missing_acv")
    if not o.is_closed:
        eligible.add("stale_open")
        if o.close_date and o.close_date < today:
            issues.append("stale_open")
            hints["days_overdue"] = (today - o.close_date).days
    return issues, eligible, hints


# ------------------------------------------------------------------ cached rows

_lock = threading.Lock()
_cache: dict = {"key": None, "deals": None, "accounts": None}


def base(session, snap) -> tuple[list[dict], list[dict]]:
    """(deal rows incl. eligibility, account flag mismatches), rebuilt per snapshot."""
    if not (_cache["key"] == snap.run_id and _cache["deals"] is not None and snap.run_id is not None):
        with _lock:
            if not (_cache["key"] == snap.run_id and _cache["deals"] is not None):
                _cache["deals"] = _build_deals(session)
                _cache["accounts"] = _build_mismatches(session, snap)
                _cache["key"] = snap.run_id
    return _cache["deals"], _cache["accounts"]


def invalidate():
    _cache["key"] = _cache["deals"] = _cache["accounts"] = None


def _build_deals(session) -> list[dict]:
    today = date.today()
    rows = []
    for o in session.query(SfdcOpportunity):
        issues, eligible, hints = deal_issues(o, today)
        if not eligible:
            continue
        rows.append({
            "id": o.id, "name": o.name, "account": data.clean_name(o.account_name),
            "account_id": o.account_id, "owner": o.owner_name, "record_type": o.record_type,
            "stage": o.stage, "outcome": deals.outcome(o),
            "close_date": o.close_date.isoformat() if o.close_date else None,
            "value": o.year1_acv if o.year1_acv is not None else o.amount,
            "issues": issues, "eligible": sorted(eligible), "hints": hints,
        })
    rows.sort(key=lambda r: (r["close_date"] or ""), reverse=True)
    return rows


def _build_mismatches(session, snap) -> list[dict]:
    """Enterprise accounts matched to ChartMogul where a flag and billing disagree."""
    out = []
    for r in enterprise.base_rows(session, snap):
        if not r["customer_uuid"]:
            continue  # nothing to compare against
        live = set()
        for u in snap.customers_by_account.get(r["id"], []):
            for sub in snap.subs_by_customer.get(u, []):
                if sub.current and sub.product:
                    live.add(sub.product)
        for ad in enterprise.ADDONS:
            if not ad["flag"]:
                continue
            products = sorted(p for p in live if enterprise._matches(ad, p))
            flag = r["addon_flags"].get(ad["key"], False)
            if flag and not products:
                problem = "Flag is set, but no live ChartMogul subscription"
            elif products and not flag:
                problem = "Billed in ChartMogul, but the flag is not set"
            else:
                continue
            out.append({"account_id": r["id"], "account": r["name"], "csm": r["csm"],
                        "customer_uuid": r["customer_uuid"], "addon": ad["label"],
                        "flag": flag, "products": products, "problem": problem,
                        "mrr": r["mrr"]})
    out.sort(key=lambda m: (-(m["mrr"] or 0), m["account"] or "", m["addon"]))
    return out


# ------------------------------------------------------------------ filtering

def filter_deals(rows, date_from: str = "", owner: str = "", issue: str = "", q: str = ""):
    """Deals eligible for at least one rule. Open deals ignore the date (stale ones are
    the point); closed deals need close_date >= date_from."""
    q = (q or "").strip().lower()
    out = []
    for r in rows:
        if date_from and r["outcome"] != "open" and (r["close_date"] or "") < date_from:
            continue
        if owner and r["owner"] != owner:
            continue
        if issue and issue not in r["eligible"]:
            continue
        if q and q not in (r["name"] or "").lower() and q not in (r["account"] or "").lower():
            continue
        out.append(r)
    return out


def filter_mismatches(rows, owner: str = "", q: str = ""):
    q = (q or "").strip().lower()
    return [m for m in rows if (not owner or m["csm"] == owner)
            and (not q or q in (m["account"] or "").lower())]


def rule_summary(deal_rows, mismatches) -> list[dict]:
    out = []
    for rule in RULES:
        if rule["key"] == "flag_mismatch":
            n, eligible = len(mismatches), None
        else:
            eligible = sum(1 for r in deal_rows if rule["key"] in r["eligible"])
            n = sum(1 for r in deal_rows if rule["key"] in r["issues"])
        out.append({**rule, "count": n, "eligible": eligible,
                    "share": (n / eligible) if eligible else None})
    return out


def by_owner(deal_rows) -> list[dict]:
    counts: dict[str, Counter] = defaultdict(Counter)
    for r in deal_rows:
        for i in r["issues"]:
            counts[r["owner"] or "(no owner)"][i] += 1
    out = [{"owner": o, "total": sum(c.values()), "counts": dict(c)} for o, c in counts.items()]
    out.sort(key=lambda x: (-x["total"], x["owner"].lower()))
    return out
