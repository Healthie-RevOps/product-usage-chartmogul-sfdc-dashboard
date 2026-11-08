"""Win/loss and competitors, over every synced Salesforce opportunity.

Replaces the SFDC "Competitor Field Opportunities" report (new-sales deals this fiscal
year with competitor + Year 1 ACV) and adds what it couldn't show: win rate and loss
reasons by competitor, across record types and any date range.

Cleanup applied here (raw values stay in sfdc_opportunities):
- Competitor: Competitor__c, else Competitor_Free_Text__c ("Primary Competitor"), else the
  first name in the free-text Competition__c. Placeholders ("Other / Unknown", "Not
  Applicable", "None (Healthie User)") count as not recorded. Spelling variants merge
  ("Avon" / "Avon Health", "Simple Practice" / "SimplePractice", ...).
- Loss reason: Add_On_Close_Lost_Reason__c, else Loss_Reason__c. The newer "R: " / "NR: "
  prefixes (regrettable / non-regrettable) become their own field, so "NR: Timing" and
  the older "Timing" count as one reason.
"""
from __future__ import annotations

import re
import threading
from collections import Counter, defaultdict
from datetime import date

from app import data
from app.db import SfdcOpportunity

NOT_RECORDED = "Not recorded"

# normalized key (lowercase alphanumerics) -> display name; None = placeholder
_ALIASES = {
    "otherunknown": None, "unknown": None, "notapplicable": None, "na": None, "none": None,
    "nonehealthieuser": None, "other": None, "tbd": None,
    "avon": "Avon Health", "avonhealth": "Avon Health",
    "simplepractice": "SimplePractice",
    "athena": "athenahealth", "athenahealth": "athenahealth",
    "ecw": "eClinicalWorks", "eclinicalworks": "eClinicalWorks",
    "epic": "Epic", "epicsystems": "Epic", "epicmychart": "Epic",
    "drchrono": "DrChrono",
    "amd": "AdvancedMD", "advancedmd": "AdvancedMD",
    "medplum": "Medplum",
    "builtinhouse": "Built in-house", "inhousesolution": "Built in-house",
    "inhouse": "Built in-house", "custombuiltemr": "Built in-house",
    "nextgen": "NextGen Healthcare", "nextgenhealthcare": "NextGen Healthcare",
    "janeapp": "Jane App", "jane": "Jane App",
    "practicebetter": "Practice Better", "practicefusion": "Practice Fusion",
}


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def norm_competitor(raw: str | None) -> str | None:
    """Canonical competitor name, or None for blanks and placeholders."""
    if not raw or not raw.strip():
        return None
    k = _key(raw)
    if k in _ALIASES:
        return _ALIASES[k]
    return re.sub(r"\s+", " ", raw).strip()


def _first_name(free_text: str | None) -> str | None:
    """First competitor in a free-text list like "Athena, eClinicalWorks, Epic"."""
    if not free_text:
        return None
    for part in re.split(r"[,;/&]|\band\b", free_text):
        name = norm_competitor(part)
        if name:
            return name
    return None


def primary_competitor(o) -> str | None:
    return (norm_competitor(o.competitor) or norm_competitor(o.competitor_text)
            or _first_name(o.competition))


_PREFIX = re.compile(r"^\s*(NR|R)\s*:\s*", re.I)


def norm_reason(raw: str | None) -> tuple[str | None, str | None]:
    """("NR: Timing") -> ("Timing", "non-regrettable"); unprefixed -> (reason, None)."""
    if not raw or not raw.strip():
        return None, None
    m = _PREFIX.match(raw)
    kind = None
    if m:
        kind = "regrettable" if m.group(1).upper() == "R" else "non-regrettable"
        raw = raw[m.end():]
    return re.sub(r"\s+", " ", raw).strip(), kind


def outcome(o) -> str:
    return "won" if o.is_won else "lost" if o.is_closed else "open"


def _value(o) -> float:
    """Year 1 ACV (what the SFDC report sums), else Amount."""
    return o.year1_acv if o.year1_acv is not None else (o.amount or 0)


# ------------------------------------------------------------------ cached rows

_lock = threading.Lock()
_cache: dict = {"key": None, "rows": None}


def base_rows(session, snap) -> list[dict]:
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
    rows = []
    for o in session.query(SfdcOpportunity):
        reason, kind = norm_reason(o.addon_loss_reason or o.loss_reason) if not o.is_won \
            and o.is_closed else (None, None)
        uuids = snap.customers_by_account.get(o.account_id or "", [])
        rows.append({
            "id": o.id, "name": o.name, "account_id": o.account_id,
            "account": data.clean_name(o.account_name), "customer_uuid": uuids[0] if uuids else None,
            "record_type": o.record_type, "type": o.opp_type, "stage": o.stage,
            "outcome": outcome(o), "close_date": o.close_date.isoformat() if o.close_date else None,
            "value": _value(o), "year1_acv": o.year1_acv, "amount": o.amount, "owner": o.owner_name,
            "competitor": primary_competitor(o) or NOT_RECORDED,
            "secondary": norm_competitor(o.secondary_competitor),
            "incumbent": norm_competitor(o.incumbent),
            "reason": reason, "regrettable": kind,
            "reason_detail": o.loss_reason_detail, "why_won": o.why_won,
            "situation": o.current_situation,
        })
    rows.sort(key=lambda r: r["close_date"] or "", reverse=True)
    return rows


# ------------------------------------------------------------------ filtering

def filter_rows(rows, record_type: str = "", date_from: str = "", date_to: str = "",
                outcome_: str = "", owner: str = "", competitor: str = "", reason: str = "",
                incumbent: str = "", q: str = "") -> list[dict]:
    q = (q or "").strip().lower()
    out = []
    for r in rows:
        if record_type and r["record_type"] != record_type:
            continue
        if date_from and (r["close_date"] or "") < date_from:
            continue
        if date_to and (r["close_date"] or "9999") > date_to:
            continue
        if outcome_ == "closed" and r["outcome"] == "open":
            continue
        if outcome_ in ("won", "lost", "open") and r["outcome"] != outcome_:
            continue
        if owner and r["owner"] != owner:
            continue
        if competitor and r["competitor"] != competitor and r["secondary"] != competitor:
            continue
        if reason and r["reason"] != reason:
            continue
        if incumbent and r["incumbent"] != incumbent:
            continue
        if q and q not in (r["name"] or "").lower() and q not in (r["account"] or "").lower():
            continue
        out.append(r)
    return out


def _win_rate(won: int, lost: int):
    return won / (won + lost) if won + lost else None


def totals(rows) -> dict:
    won = [r for r in rows if r["outcome"] == "won"]
    lost = [r for r in rows if r["outcome"] == "lost"]
    opn = [r for r in rows if r["outcome"] == "open"]
    closed = won + lost
    return {
        "deals": len(rows), "won": len(won), "lost": len(lost), "open": len(opn),
        "win_rate": _win_rate(len(won), len(lost)),
        "won_value": sum(r["value"] or 0 for r in won),
        "lost_value": sum(r["value"] or 0 for r in lost),
        "open_value": sum(r["value"] or 0 for r in opn),
        "competitor_recorded": (sum(1 for r in closed if r["competitor"] != NOT_RECORDED) / len(closed)
                                if closed else None),
        "reason_recorded": (sum(1 for r in lost if r["reason"]) / len(lost) if lost else None),
    }


def by_competitor(rows, limit: int = 40) -> list[dict]:
    """Per primary competitor: won / lost / open, win rate, values, top loss reasons, and
    how often it shows up as the secondary competitor."""
    groups: dict[str, list] = defaultdict(list)
    secondary = Counter()
    for r in rows:
        groups[r["competitor"]].append(r)
        if r["secondary"] and r["secondary"] != r["competitor"]:
            secondary[r["secondary"]] += 1
    out = []
    for name, rs in groups.items():
        won = sum(1 for r in rs if r["outcome"] == "won")
        lost = [r for r in rs if r["outcome"] == "lost"]
        reasons = Counter(r["reason"] for r in lost if r["reason"])
        out.append({
            "competitor": name, "deals": len(rs), "won": won, "lost": len(lost),
            "open": sum(1 for r in rs if r["outcome"] == "open"),
            "win_rate": _win_rate(won, len(lost)),
            "won_value": sum(r["value"] or 0 for r in rs if r["outcome"] == "won"),
            "lost_value": sum(r["value"] or 0 for r in lost),
            "top_reasons": [{"reason": k, "count": v} for k, v in reasons.most_common(3)],
            "secondary": secondary.get(name, 0),
        })
    # real competitors by deal count; "Not recorded" last
    out.sort(key=lambda g: (g["competitor"] == NOT_RECORDED, -g["deals"], g["competitor"].lower()))
    return out[:limit] + [g for g in out[limit:] if g["competitor"] == NOT_RECORDED]


def by_reason(rows) -> list[dict]:
    lost = [r for r in rows if r["outcome"] == "lost"]
    groups: dict[str, list] = defaultdict(list)
    for r in lost:
        groups[r["reason"] or NOT_RECORDED].append(r)
    out = []
    for name, rs in groups.items():
        comps = Counter(r["competitor"] for r in rs if r["competitor"] != NOT_RECORDED)
        kinds = Counter(r["regrettable"] for r in rs if r["regrettable"])
        out.append({
            "reason": name, "lost": len(rs), "share": len(rs) / len(lost),
            "lost_value": sum(r["value"] or 0 for r in rs),
            "regrettable": kinds.get("regrettable", 0),
            "non_regrettable": kinds.get("non-regrettable", 0),
            "top_competitor": comps.most_common(1)[0][0] if comps else None,
        })
    out.sort(key=lambda g: (g["reason"] == NOT_RECORDED, -g["lost"]))
    return out


def facets(rows) -> dict:
    def vals(k):
        return sorted({r[k] for r in rows if r[k]}, key=str.lower)
    comp_counts = Counter(r["competitor"] for r in rows if r["competitor"] != NOT_RECORDED)
    return {"record_type": vals("record_type"), "owner": vals("owner"),
            "competitor": [c for c, _ in comp_counts.most_common()] + [NOT_RECORDED],
            "reason": vals("reason"), "incumbent": vals("incumbent")}
