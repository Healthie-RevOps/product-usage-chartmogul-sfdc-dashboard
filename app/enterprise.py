"""Add-on adoption, by plan (Enterprise by default; see app/segments.py).

One row per Salesforce account on the chosen plan, one column per add-on, each cell in one state:

  has       billed today: SFDC Stripe_Has_* flag, or a live ChartMogul subscription to the
            add-on's product, or a deal won in the last WON_GRACE_DAYS not billed yet
  pipeline  an open opportunity for the add-on
  lost      the most recent add-on opportunity was closed lost (reason, revisit date)
  none      never pitched (or won long ago and no longer billed; see the cell note)

Replaces three SFDC reports (Enterprise Customer Attribute Reporting; Scribe/Bridge/Vis+
Open Opportunities; Add-On Closed Lost | Loss Reasons) with one view.

Which add-on an opportunity is for: Expansion_Product__c when it names one (it's blank on
most deals), else patterns over the opp name and product text fields. The SFDC report
matched on opp name alone ("contains Scribe,Bridge,visualize").
"""
from __future__ import annotations

import re
import threading
from collections import defaultdict
from datetime import date, timedelta

from app import data, segments
from app.db import SfdcAccount, SfdcContact, SfdcOpportunity

WON_GRACE_DAYS = 120

# key, label, SFDC flag column (None if SFDC has no flag), pattern over ChartMogul product
# families and opp text, optional exclude pattern, Expansion_Product__c values.
ADDONS = [
    {"key": "ai_scribe", "label": "AI Scribe", "flag": "has_ai_scribe",
     "rx": r"scribe", "picklist": ["AI Scribe"]},
    {"key": "bridge", "label": "Bridge", "flag": None,
     "rx": r"\bbridge\b", "picklist": ["Bridge"]},
    {"key": "visualize", "label": "Visualize+", "flag": None,
     "rx": r"visuali[sz]|\bvis\+|\bvis\b", "picklist": []},
    {"key": "api", "label": "API access", "flag": "has_api_access",
     "rx": r"\bapi\b|\bsdk\b|\bfhir\b", "picklist": ["Full API and SDK Access"]},
    {"key": "erx", "label": "E-Rx", "flag": "has_erx",
     "rx": r"e-?rx\b|dosespot|e-?prescri|\bepcs\b", "picklist": []},
    # "White label" without "semi" counts as full white label
    {"key": "full_wl", "label": "Full white label", "flag": "has_full_white_label",
     "rx": r"white[\s-]*label|whitelabel|\bwl\b", "exclude": r"semi",
     "picklist": ["Full Web White Label & Customizations",
                  "Full Mobile White Label & Customizations"]},
    {"key": "semi_wl", "label": "Semi white label", "flag": "has_semi_white_label",
     "rx": r"semi[\s-]*(web\s*)?(white[\s-]*label|whitelabel)", "picklist": ["Semi Web White Label"]},
    {"key": "sub_org", "label": "Sub-orgs", "flag": "has_sub_org",
     "rx": r"sub[\s-]*org", "picklist": []},
    {"key": "zoom", "label": "Zoom", "flag": "has_zoom", "rx": r"\bzoom\b", "picklist": []},
    {"key": "hubspot", "label": "HubSpot", "flag": "has_hubspot", "rx": r"hubspot", "picklist": []},
    {"key": "shopify", "label": "Shopify", "flag": "has_shopify", "rx": r"shopify", "picklist": []},
]
ADDON_KEYS = [a["key"] for a in ADDONS]
STATES = ("has", "pipeline", "lost", "none")
STATE_FILTERS = STATES + ("not_has",)
for _a in ADDONS:
    _a["_rx"] = re.compile(_a["rx"], re.I)
    _a["_ex"] = re.compile(_a["exclude"], re.I) if _a.get("exclude") else None
_PICKLIST = {v: a["key"] for a in ADDONS for v in a["picklist"]}


def _matches(addon: dict, text: str | None) -> bool:
    return bool(text) and bool(addon["_rx"].search(text)) and not (
        addon["_ex"] and addon["_ex"].search(text))


def detect_addons(opp) -> set[str]:
    """Add-ons an opportunity is for (can be several: "Scribe + Bridge")."""
    if opp.expansion_product in _PICKLIST:
        return {_PICKLIST[opp.expansion_product]}
    text = " ".join(t for t in (opp.name, opp.expansion_product_text, opp.additional_products) if t)
    return {a["key"] for a in ADDONS if _matches(a, text)}


def _iso(d):
    return d.isoformat() if d else None


def _opp_info(o) -> dict:
    return {"opp_id": o.id, "opp_name": o.name, "stage": o.stage, "close_date": _iso(o.close_date),
            "amount": o.amount, "arr_delta": o.arr_delta, "probability": o.probability,
            "owner": o.owner_name}


def _cell(addon: dict, acct, cm_products: list[str], opps: list, today: date) -> dict:
    sources = []
    if addon["flag"] and getattr(acct, addon["flag"]):
        sources.append("Salesforce")
    if cm_products:
        sources.append("ChartMogul (" + ", ".join(sorted(set(cm_products))[:2]) + ")")
    open_ = [o for o in opps if not o.is_closed]
    won = [o for o in opps if o.is_won]
    lost = [o for o in opps if o.is_closed and not o.is_won]
    latest_won = max(won, key=lambda o: o.close_date or date.min) if won else None
    if sources:
        cell = {"state": "has", "source": " + ".join(sources)}
        if open_:
            cell["note"] = f"{len(open_)} open deal(s) too"
    elif latest_won and latest_won.close_date and \
            latest_won.close_date >= today - timedelta(days=WON_GRACE_DAYS):
        cell = {"state": "has", "source": "Won deal, not billed yet", **_opp_info(latest_won)}
    elif open_:
        o = min(open_, key=lambda o: o.close_date or date.max)
        cell = {"state": "pipeline", **_opp_info(o)}
    elif lost:
        o = max(lost, key=lambda o: o.close_date or date.min)
        cell = {"state": "lost", **_opp_info(o),
                "reason": o.addon_loss_reason or o.loss_reason,
                "revisit_date": _iso(o.revisit_date),
                "competitor": o.competitor_text or o.competitor,
                "value_gap": o.value_gap, "target_price": o.target_price}
    else:
        cell = {"state": "none"}
        if latest_won:
            cell["note"] = f"Won {_iso(latest_won.close_date)}, not billed now"
    cell["deals"] = len(opps)
    return cell


# ------------------------------------------------------------------ cached base rows

_lock = threading.Lock()
_cache: dict = {"key": None, "rows": None}


def base_rows(session, snap) -> list[dict]:
    """Every Enterprise account with its add-on cells. Rebuilt when the snapshot changes."""
    if _cache["key"] == snap.run_id and _cache["rows"] is not None and snap.run_id is not None:
        return _cache["rows"]
    with _lock:
        if not (_cache["key"] == snap.run_id and _cache["rows"] is not None):
            _cache["rows"] = _build(session, snap)
            _cache["key"] = snap.run_id
    return _cache["rows"]


def invalidate():
    _cache["key"] = _cache["rows"] = None


def _build(session, snap) -> list[dict]:
    today = date.today()
    accts = []
    for a in session.query(SfdcAccount):
        plan = data.account_plan(snap, a.id)
        if plan:
            accts.append((a, plan))
    ids = [a.id for a, _ in accts]
    opps_by_acct: dict[str, list] = defaultdict(list)
    for i in range(0, len(ids), 500):
        for o in session.query(SfdcOpportunity).filter(SfdcOpportunity.account_id.in_(ids[i:i + 500])):
            opps_by_acct[o.account_id].append(o)

    rows = []
    for a, plan in accts:
        # live ChartMogul products on this account's customers
        cm: dict[str, list[str]] = defaultdict(list)
        uuids = snap.customers_by_account.get(a.id, [])
        for u in uuids:
            for sub in snap.subs_by_customer.get(u, []):
                if sub.current and sub.product:
                    for ad in ADDONS:
                        if _matches(ad, sub.product):
                            cm[ad["key"]].append(sub.product)
        by_addon: dict[str, list] = defaultdict(list)
        for o in opps_by_acct.get(a.id, []):
            for k in detect_addons(o):
                by_addon[k].append(o)
        stats = snap.accounts.get(a.id) or {}
        rows.append({
            "id": a.id, "name": data.clean_name(a.name), "sfdc_name": a.name,
            "customer_uuid": uuids[0] if uuids else None,
            "customer_status": a.customer_status, "plan": plan,
            "mrr": plan_mrr(snap, a.id, a.sfdc_mrr, plan),
            "implementation_status": a.implementation_status, "csm": a.csm_name,
            "segment": a.account_segment, "renewal_date": _iso(a.renewal_date),
            "website": a.website, "org_id": a.healthie_org_id,
            "org_count": a.healthie_org_count, "kickoff_date": _iso(a.kickoff_date),
            "active_standard_users": a.active_standard_users,
            "providers": stats.get("hl_active_providers"),
            "active_users": stats.get("active_users"),
            "flags": {col: bool(getattr(a, col)) for col in (
                "has_active_client", "has_standard_seats", "has_support_seats")},
            "addon_flags": {ad["key"]: bool(getattr(a, ad["flag"])) for ad in ADDONS if ad["flag"]},
            "addons": {ad["key"]: _cell(ad, a, cm.get(ad["key"], []),
                                        by_addon.get(ad["key"], []), today) for ad in ADDONS},
        })
    rows.sort(key=lambda r: (-(r["mrr"] or 0), (r["name"] or "").lower()))
    return rows


# ------------------------------------------------------------------ filtering

def parse_addon_filters(items: list[str]) -> dict[str, str]:
    """["ai_scribe:none", ...] -> {"ai_scribe": "none"}; unknown keys/states ignored."""
    out = {}
    for it in items or []:
        key, _, state = it.partition(":")
        if key in ADDON_KEYS and state in STATE_FILTERS:
            out[key] = state
    return out


def plan_mrr(snap, account_id: str, sfdc_mrr, plan: str | None):
    """Enterprise: Salesforce MRR (what the SFDC Enterprise report sums); other plans:
    ChartMogul billing, which is kept current for self-serve customers."""
    if plan == segments.ENTERPRISE and sfdc_mrr is not None:
        return sfdc_mrr
    return data.account_mrr(snap, account_id, sfdc_mrr)


def filter_rows(rows: list[dict], paying: bool = True, status: str = "", csm: str = "",
                segment: str = "", impl: str = "", q: str = "",
                addon_filters: dict[str, str] | None = None,
                account_ids: list[str] | None = None,
                plan: str = segments.ENTERPRISE) -> list[dict]:
    q = (q or "").strip().lower()
    keep = set(account_ids) if account_ids is not None else None
    out = []
    for r in rows:
        if not segments.matches(r["plan"], plan):
            continue
        if paying and not (r["mrr"] or 0) > 0:
            continue
        if status and r["customer_status"] != status:
            continue
        if csm and r["csm"] != csm:
            continue
        if segment and r["segment"] != segment:
            continue
        if impl and r["implementation_status"] != impl:
            continue
        if q and q not in (r["sfdc_name"] or "").lower():
            continue
        if keep is not None and r["id"] not in keep:
            continue
        ok = True
        for key, want in (addon_filters or {}).items():
            st = r["addons"][key]["state"]
            if (want == "not_has" and st == "has") or (want != "not_has" and st != want):
                ok = False
                break
        if ok:
            out.append(r)
    return out


def summary(rows: list[dict]) -> list[dict]:
    """Per add-on: accounts in each state, plus open pipeline amount."""
    out = []
    for ad in ADDONS:
        counts = {s: 0 for s in STATES}
        pipeline_amount = 0.0
        for r in rows:
            c = r["addons"][ad["key"]]
            counts[c["state"]] += 1
            if c["state"] == "pipeline":
                pipeline_amount += c.get("amount") or 0
        out.append({"key": ad["key"], "label": ad["label"], "counts": counts,
                    "pipeline_amount": pipeline_amount,
                    "has_flag": bool(ad["flag"])})
    return out


def totals(rows: list[dict]) -> dict:
    cells = [c for r in rows for c in r["addons"].values()]
    return {
        "accounts": len(rows),
        "mrr": sum(r["mrr"] or 0 for r in rows),
        "pipeline": sum(1 for c in cells if c["state"] == "pipeline"),
        "pipeline_amount": sum(c.get("amount") or 0 for c in cells if c["state"] == "pipeline"),
        "lost": sum(1 for c in cells if c["state"] == "lost"),
        "none": sum(1 for c in cells if c["state"] == "none"),
    }


def facets(rows: list[dict]) -> dict:
    def vals(k):
        return sorted({r[k] for r in rows if r[k]})
    return {"status": vals("customer_status"), "csm": vals("csm"), "segment": vals("segment"),
            "impl": vals("implementation_status")}


# ------------------------------------------------------------------ campaign contacts

def campaign_contacts(session, rows: list[dict]) -> list[dict]:
    """Providers and admins at these accounts: active Healthie users with a Standard
    (provider) seat or an owner/admin role, with an email, not opted out of email."""
    by_id = {r["id"]: r for r in rows}
    ids = list(by_id)
    out = []
    for i in range(0, len(ids), 500):
        q = (session.query(SfdcContact)
             .filter(SfdcContact.account_id.in_(ids[i:i + 500]))
             .filter(SfdcContact.user_active.is_(True))
             .filter(SfdcContact.email.isnot(None), SfdcContact.email != "")
             .filter((SfdcContact.email_opt_out.is_(False)) | (SfdcContact.email_opt_out.is_(None)))
             .filter((SfdcContact.user_type == "Standard") | (SfdcContact.is_org_owner.is_(True))
                     | (SfdcContact.is_org_admin.is_(True))))
        for ct in q:
            out.append({"contact": ct, "account": by_id[ct.account_id]})
    out.sort(key=lambda x: ((x["account"]["name"] or "").lower(), not x["contact"].is_org_owner,
                            not x["contact"].is_org_admin, (x["contact"].name or "").lower()))
    return out
