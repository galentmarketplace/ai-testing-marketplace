"""OIDC (OpenID Connect) sign-in — enterprise SSO alongside GitHub OAuth.

Customers authenticate with their own identity provider (Auth0, Okta, Entra ID, Google Workspace…).
We use the authorization-code flow: the platform holds the client secret, exchanges the code for an
ID token, and verifies that token's signature against the provider's published JWKS — never trusting
its contents unverified.

Configure with ATM_OIDC_ISSUER / ATM_OIDC_CLIENT_ID / ATM_OIDC_CLIENT_SECRET. When those are absent
the feature is simply off and GitHub OAuth remains the only sign-in.
"""
import json
import os
import time
import urllib.parse
import urllib.request

_DISCOVERY_TTL = 3600
_cache: dict = {}


def configured() -> bool:
    return bool(os.environ.get("ATM_OIDC_ISSUER") and os.environ.get("ATM_OIDC_CLIENT_ID")
                and os.environ.get("ATM_OIDC_CLIENT_SECRET"))


def issuer() -> str:
    return (os.environ.get("ATM_OIDC_ISSUER") or "").rstrip("/")


def discovery() -> dict:
    """The provider's OIDC metadata, cached for an hour."""
    now = time.time()
    if _cache.get("doc") and now - _cache.get("at", 0) < _DISCOVERY_TTL:
        return _cache["doc"]
    with urllib.request.urlopen(issuer() + "/.well-known/openid-configuration", timeout=15) as r:
        doc = json.loads(r.read())
    _cache.update(doc=doc, at=now)
    return doc


def authorize_url(redirect_uri: str, state: str, nonce: str) -> str:
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": os.environ["ATM_OIDC_CLIENT_ID"],
        "redirect_uri": redirect_uri, "scope": "openid profile email",
        "state": state, "nonce": nonce,
    })
    return f"{discovery()['authorization_endpoint']}?{q}"


def exchange_code(code: str, redirect_uri: str) -> dict:
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": os.environ["ATM_OIDC_CLIENT_ID"],
        "client_secret": os.environ["ATM_OIDC_CLIENT_SECRET"],
    }).encode()
    req = urllib.request.Request(discovery()["token_endpoint"], data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def verify_id_token(id_token: str, nonce: str | None = None) -> dict:
    """Verify signature (via the provider's JWKS), issuer, audience and nonce. Returns the claims."""
    import jwt
    from jwt import PyJWKClient
    key = PyJWKClient(discovery()["jwks_uri"], cache_keys=True).get_signing_key_from_jwt(id_token).key
    claims = jwt.decode(id_token, key, algorithms=["RS256"],
                        audience=os.environ["ATM_OIDC_CLIENT_ID"],
                        issuer=discovery()["issuer"])
    if nonce is not None and claims.get("nonce") != nonce:
        raise ValueError("OIDC nonce mismatch — possible replay")
    return claims


def principal_from_claims(claims: dict) -> dict:
    """Map ID-token claims to our user shape. `login` is the stable subject identity we authorize on."""
    login = claims.get("email") or claims.get("preferred_username") or claims.get("sub")
    return {"login": login, "name": claims.get("name") or login,
            "avatar": claims.get("picture") or "", "sub": claims.get("sub"),
            "email": claims.get("email"), "idp": "oidc"}


def logout_url(return_to: str) -> str | None:
    end = discovery().get("end_session_endpoint")
    if not end:
        return None
    return end + "?" + urllib.parse.urlencode({"client_id": os.environ["ATM_OIDC_CLIENT_ID"],
                                               "returnTo": return_to, "post_logout_redirect_uri": return_to})
