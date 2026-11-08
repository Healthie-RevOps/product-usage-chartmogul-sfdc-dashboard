"""Enterprise account data: is each account's Salesforce data complete (CSM, segment,
renewal, references, case study, logo...), with a few reference fields editable.

Edits write straight to Salesforce (sync/sfdc.py update_account), but only:
  - fields in EDITABLE (mirrors sfdc.ACCOUNT_EDITABLE_FIELDS), with validated values
  - by people in SFDC_EDITORS (comma-separated emails; "*" = anyone signed in)
  - if Salesforce still holds the value the person saw (else: conflict, nothing written)
Every attempt is logged in account_edits with the person's email, because Salesforce
records the change as the app's integration login.
"""
from __future__ import annotations

import os
from datetime import date

from app import data
from app.db import AccountEdit, SfdcAccount
from sync import sfdc

YES_NO = ["Yes", "No"]

# key -> label, SfdcAccount attribute, kind (text | date | yesno | bool), group
FIELDS = [
    {"key": "csm", "attr": "csm_name", "label": "CSM", "kind": "text", "group": "Account"},
    {"key": "segment", "attr": "account_segment", "label": "Segment", "kind": "text", "group": "Account"},
    {"key": "implementation_status", "attr": "implementation_status", "label": "Implementation", "kind": "text", "group": "Account"},
    {"key": "renewal_date", "attr": "renewal_date", "label": "Renewal date", "kind": "date", "group": "Account"},
    {"key": "kickoff_date", "attr": "kickoff_date", "label": "Kickoff date", "kind": "date", "group": "Account"},
    {"key": "website", "attr": "website", "label": "Website", "kind": "text", "group": "Account"},
    {"key": "org_id", "attr": "healthie_org_id", "label": "Healthie Org ID", "kind": "text", "group": "Account"},
    {"key": "referenceable", "attr": "referenceable", "label": "Referenceable", "kind": "yesno", "group": "References"},
    {"key": "reference_date", "attr": "reference_date", "label": "Reference date", "kind": "date", "group": "References"},
    {"key": "contracted_case_study", "attr": "contracted_case_study", "label": "Contracted to case study", "kind": "yesno", "group": "References"},
    {"key": "case_study_complete", "attr": "case_study_complete", "label": "Case study complete", "kind": "yesno", "group": "References"},
    {"key": "case_study_available", "attr": "case_study_available", "label": "Case study available", "kind": "bool", "group": "References"},
    {"key": "contracted_logo", "attr": "contracted_logo", "label": "Contracted to use logo", "kind": "yesno", "group": "References"},
    {"key": "logo_use_permitted", "attr": "logo_use_permitted", "label": "Logo use permitted", "kind": "text", "group": "References"},
]
FIELD = {f["key"]: f for f in FIELDS}
EDITABLE = {k: v for k, v in sfdc.ACCOUNT_EDITABLE_FIELDS.items() if k in FIELD}
for _f in FIELDS:
    _f["editable"] = _f["key"] in EDITABLE
FILTER_IS = ("missing", "filled", "yes", "no")


# ------------------------------------------------------------------ permissions

def editors() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("SFDC_EDITORS", "").split(",") if e.strip()}


def can_edit(user: dict | None) -> bool:
    """Signed-in people on the SFDC_EDITORS list ("*" = anyone signed in). Machine
    (Basic auth) callers have no user and can never write."""
    if not user or not user.get("email"):
        return False
    allowed = editors()
    return "*" in allowed or user["email"].lower() in allowed


# ------------------------------------------------------------------ values

def display(field: dict, raw) -> str | None:
    """Normalized text for a field value; None = blank."""
    if raw is None or raw == "":
        return None
    if field["kind"] == "bool":
        return "Yes" if raw else None        # an unticked checkbox means "not yet"
    if field["kind"] == "date":
        return raw.isoformat() if isinstance(raw, date) else str(raw)[:10]
    return str(raw).strip() or None


def rows(session, snap, paying: bool = True, status: str = "", csm: str = "", segment: str = "",
         impl: str = "", q: str = "", field: str = "", is_: str = "",
         account_ids: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    """(all Enterprise rows, filtered rows). Read straight from the DB (a few hundred
    rows), so a saved edit shows up immediately."""
    q = (q or "").strip().lower()
    keep = set(account_ids) if account_ids is not None else None
    base, out = [], []
    for a in session.query(SfdcAccount).filter(SfdcAccount.current_plan == "Enterprise"):
        uuids = snap.customers_by_account.get(a.id, [])
        values = {f["key"]: display(f, getattr(a, f["attr"])) for f in FIELDS}
        r = {"id": a.id, "name": data.clean_name(a.name), "sfdc_name": a.name,
             "customer_uuid": uuids[0] if uuids else None, "mrr": a.sfdc_mrr,
             "customer_status": a.customer_status, "csm": a.csm_name, "segment": a.account_segment,
             "implementation_status": a.implementation_status, "values": values,
             "missing": sum(1 for v in values.values() if v is None)}
        base.append(r)
        if paying and not (a.sfdc_mrr or 0) > 0:
            continue
        if status and a.customer_status != status:
            continue
        if csm and a.csm_name != csm:
            continue
        if segment and a.account_segment != segment:
            continue
        if impl and a.implementation_status != impl:
            continue
        if q and q not in (a.name or "").lower():
            continue
        if keep is not None and a.id not in keep:
            continue
        if field in FIELD and is_ in FILTER_IS:
            v = values[field]
            if (is_ == "missing" and v is not None) or (is_ == "filled" and v is None) \
                    or (is_ == "yes" and (v or "").lower() != "yes") \
                    or (is_ == "no" and (v or "").lower() != "no"):
                continue
        out.append(r)
    out.sort(key=lambda r: (-(r["mrr"] or 0), (r["name"] or "").lower()))
    return base, out


def summary(rows_: list[dict]) -> list[dict]:
    n = len(rows_)
    out = []
    for f in FIELDS:
        vals = [r["values"][f["key"]] for r in rows_]
        filled = sum(1 for v in vals if v is not None)
        out.append({"key": f["key"], "label": f["label"], "group": f["group"], "kind": f["kind"],
                    "editable": f["editable"], "filled": filled, "missing": n - filled,
                    "yes": sum(1 for v in vals if (v or "").lower() == "yes"),
                    "share": filled / n if n else None})
    return out


# ------------------------------------------------------------------ editing

class EditError(ValueError):
    pass


def _clean(field: dict, value) -> str | None:
    """Validated value to write (None = clear the field)."""
    value = (value or "").strip() if isinstance(value, str) else value
    if value in (None, ""):
        return None
    if field["kind"] == "yesno":
        if value not in YES_NO:
            raise EditError(f"{field['label']} must be Yes or No")
        return value
    if field["kind"] == "date":
        try:
            return date.fromisoformat(value).isoformat()
        except (TypeError, ValueError):
            raise EditError(f"{field['label']} must be a date (YYYY-MM-DD)")
    raise EditError(f"{field['label']} can't be edited here")


def _norm(field: dict, v) -> str | None:
    return display(field, v)


def edit(session, user: dict, account_id: str, key: str, value, expected) -> dict:
    """Write one field to Salesforce. Returns {"status": saved | conflict, "value": ...}.
    Raises EditError for bad input / permissions; sfdc.SfdcError if Salesforce refuses."""
    if not can_edit(user):
        raise EditError("You don't have edit access. Ask an admin to add you to SFDC_EDITORS.")
    if key not in EDITABLE:
        raise EditError("That field can't be edited from the dashboard")
    field = FIELD[key]
    api = EDITABLE[key]
    acct = session.get(SfdcAccount, account_id)
    if acct is None or acct.current_plan != "Enterprise":
        raise EditError("Unknown Enterprise account")
    new = _clean(field, value)
    log = AccountEdit(user_email=user["email"].lower(), account_id=acct.id, account_name=acct.name,
                      field=key, sfdc_field=api, new_value=new)
    try:
        current = _norm(field, sfdc.get_account(acct.id, [api]).get(api))
        log.old_value = current
        if current != _norm(field, expected):
            # Someone changed it in Salesforce since the last sync: show them, write nothing
            log.status = "conflict"
            setattr(acct, field["attr"], _to_attr(field, current))
            return {"status": "conflict", "value": current}
        if current == new:
            log.status = "saved"
            return {"status": "saved", "value": new}
        sfdc.update_account(acct.id, {api: new})
        setattr(acct, field["attr"], _to_attr(field, new))
        log.status = "saved"
        return {"status": "saved", "value": new}
    except sfdc.SfdcError as exc:
        log.status, log.error = "error", str(exc)[:2000]
        raise
    finally:
        session.add(log)


def _to_attr(field: dict, v: str | None):
    if v is None:
        return None
    return date.fromisoformat(v) if field["kind"] == "date" else v


def recent_edits(session, limit: int = 50) -> list[dict]:
    out = []
    for e in session.query(AccountEdit).order_by(AccountEdit.id.desc()).limit(limit):
        out.append({"at": e.at.isoformat() if e.at else None, "user": e.user_email,
                    "account": data.clean_name(e.account_name), "account_id": e.account_id,
                    "field": FIELD.get(e.field, {}).get("label", e.field),
                    "old": e.old_value, "new": e.new_value, "status": e.status, "error": e.error})
    return out
