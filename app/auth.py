"""@gethealthie.com-only access.

Sign-in is Google Identity Services: the browser gets a Google ID token, the
server verifies it (signature, audience = our client ID, verified email in the
allowed domain) and stores the email in a signed session cookie. Only the
public client ID is needed -- no OAuth client secret.

The gate fails CLOSED: without SESSION_SECRET nothing is served, unless
ALLOW_INSECURE_NO_AUTH=1 is set for local development.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

# The shared Healthie internal-apps OAuth client (also used by the creative
# hub, customer-comms and commissions). Client IDs are public by design.
DEFAULT_CLIENT_ID = "1086850412791-1pb8prd0ebpoaevi7513uvufougqh7kt.apps.googleusercontent.com"
INSECURE_OVERRIDE = "ALLOW_INSECURE_NO_AUTH"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def client_id() -> str:
    return _env("GOOGLE_CLIENT_ID", DEFAULT_CLIENT_ID)


def allowed_domain() -> str:
    return _env("ALLOWED_EMAIL_DOMAIN", "gethealthie.com").lower().lstrip("@")


def insecure() -> bool:
    return os.environ.get(INSECURE_OVERRIDE) == "1"


def configured() -> bool:
    return bool(_env("SESSION_SECRET")) or insecure()


def email_allowed(email: str | None) -> bool:
    """Suffix match on "@domain" so `x@gethealthie.com.evil.com` cannot pass."""
    return bool(email) and str(email).strip().lower().endswith("@" + allowed_domain())


def verify_google_credential(credential: str) -> dict:
    """Verify a GIS ID token and return {email, name}. Raises ValueError."""
    claims = id_token.verify_oauth2_token(credential, google_requests.Request(), client_id())
    email = (claims.get("email") or "").lower()
    if not claims.get("email_verified"):
        raise ValueError("Google has not verified this email address.")
    if not email_allowed(email) or claims.get("hd", "").lower() != allowed_domain():
        raise ValueError(f"Use your @{allowed_domain()} Google account.")
    return {"email": email, "name": claims.get("name") or email}


def current_user(request: Request) -> dict | None:
    if insecure():
        email = _env("DEV_USER_EMAIL", "dev@" + allowed_domain())
        return {"email": email, "name": email}
    user = request.session.get("user")
    if user and email_allowed(user.get("email")):
        return user
    return None


def require_user(request: Request) -> dict:
    """API dependency: the signed-in user, or 401.

    Mutating requests must also carry `X-Requested-With: fetch`. Browsers will
    not send a custom header cross-site without a CORS preflight, which this
    app never grants, so a forged form post from another site is refused.
    """
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sign in required.")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403, "Missing request header.")
    return user
