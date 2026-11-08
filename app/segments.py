"""Plan segments: which plan a customer (or account) is on, from what ChartMogul bills.

  Enterprise        Enterprise base fee / Enterprise* products, Standard or Support Seats
  Group + API       Group+ Plan, or Group Plan together with API Access
  Group             Group Plan
  Private practice  Plus, Essentials, Small Practice, Core, Starter, Beginnings, Student, Practice Plus

ChartMogul (billing) decides. Salesforce's Current_Plan__c is only a fallback for accounts
with no ChartMogul match: it is stale for private practice (2026-11-08: 3,886 paying accounts
still labelled "Plus Trial"). Customers billed only for add-ons fall back the same way, else
they are not in any plan segment.
"""
from __future__ import annotations

import re

ENTERPRISE, GROUP_API, GROUP, PRIVATE = "Enterprise", "Group + API", "Group", "Private practice"
PLANS = [ENTERPRISE, GROUP_API, GROUP, PRIVATE]
ALL = "All plans"

_ENTERPRISE = re.compile(r"^enterprise|^standard seat|^support seat", re.I)
_GROUP_PLUS = re.compile(r"^group\s*\+|^group\+", re.I)
_GROUP = re.compile(r"^group", re.I)
_API = re.compile(r"\bapi\b", re.I)
_PRIVATE = re.compile(r"^(plus|essentials|small practice|core|starter|beginnings|student|practice plus)", re.I)
_ADDON_ONLY = re.compile(r"visuali[sz]", re.I)      # "Visualize+ Enterprise" is an add-on

_SFDC = {"Enterprise": ENTERPRISE, "Group": GROUP, "Plus": PRIVATE, "Plus Trial": PRIVATE,
         "Essentials": PRIVATE, "Core": PRIVATE, "Small Practices": PRIVATE, "Starter": PRIVATE,
         "Beginnings": PRIVATE, "Student": PRIVATE, "Practice Plus": PRIVATE,
         "DHS Plus Trial": PRIVATE}


def from_products(products) -> str | None:
    """Plan segment for a set of ChartMogul product families held at one point in time."""
    fams = [p for p in products if p]
    if any(_ENTERPRISE.search(p) and not _ADDON_ONLY.search(p) for p in fams):
        return ENTERPRISE
    if any(_GROUP_PLUS.search(p) for p in fams):
        return GROUP_API
    has_group = any(_GROUP.search(p) for p in fams)
    if has_group and any(_API.search(p) for p in fams):
        return GROUP_API
    if has_group:
        return GROUP
    if any(_PRIVATE.search(p) for p in fams):
        return PRIVATE
    return None


def from_sfdc(current_plan: str | None, has_api: bool = False) -> str | None:
    plan = _SFDC.get((current_plan or "").strip())
    if plan == GROUP and has_api:
        return GROUP_API
    return plan


def matches(plan: str | None, wanted: str) -> bool:
    """Filter helper: wanted is one of PLANS or ALL / ""."""
    return not wanted or wanted == ALL or plan == wanted
