"""Security tests for the JWT/JWKS auth path (PROD_DEPLOYMENT_TODO §4, AUTH-JWT-TEST).

Covers signature verification (RS256/ES256 accept; alg=none / disallowed-alg /
wrong-kid / tampered-payload / wrong-key reject), claim validation
(exp/nbf/iss/aud/tid), the non-dev environment hard-guards, and the JWKS cache.

Keys are generated in-process with ``cryptography``; tokens and JWKs are built
with ``PyJWT``. No network is touched (the one JWKS-URL test stubs ``urlopen``).

Runs standalone (``python test_auth.py``) or under pytest.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from v1.utils.auth import (
    AuthConfig,
    AuthConfigurationError,
    AuthValidationError,
    JwtAuthenticator,
    _decode_jwt,
    _extract_bearer_token,
)

# -- key material (generated once; RSA keygen is the slow part) ----------------

_RSA_KID = "test-rsa"
_EC_KID = "test-ec"
_RSA_PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_RSA_PRIV_OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_EC_PRIV = ec.generate_private_key(ec.SECP256R1())


def _public_jwk(private_key, kid: str) -> dict:
    pub = private_key.public_key()
    if isinstance(private_key, rsa.RSAPrivateKey):
        jwk = json.loads(RSAAlgorithm.to_jwk(pub))
    else:
        jwk = json.loads(ECAlgorithm.to_jwk(pub))
    jwk["kid"] = kid
    return jwk


_RSA_JWK = _public_jwk(_RSA_PRIV, _RSA_KID)
_RSA_JWK_OTHER = _public_jwk(_RSA_PRIV_OTHER, "test-rsa-2")
_EC_JWK = _public_jwk(_EC_PRIV, _EC_KID)


# -- helpers -------------------------------------------------------------------


def _authenticator(*jwks_keys, algorithms=("RS256",), **cfg) -> JwtAuthenticator:
    config = AuthConfig(
        mode="jwt",
        algorithms=algorithms,
        jwks_json=json.dumps({"keys": list(jwks_keys)}) if jwks_keys else None,
        **cfg,
    )
    return JwtAuthenticator(config)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _expect(exc_type, substring, fn) -> None:
    try:
        fn()
    except exc_type as exc:
        assert substring in str(exc), f"expected {substring!r} in {exc!r}"
        return
    raise AssertionError(f"expected {exc_type.__name__} containing {substring!r}")


@contextlib.contextmanager
def _env(**overrides):
    saved = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _decode(token: str):
    return _decode_jwt(token)


# -- signature: accept valid ---------------------------------------------------


def test_signature_accepts_valid_rs256() -> None:
    auth = _authenticator(_RSA_JWK)
    token = jwt.encode({"sub": "x"}, _RSA_PRIV, algorithm="RS256", headers={"kid": _RSA_KID})
    header, _claims, signing_input, signature = _decode(token)
    auth._validate_signature(header, signing_input, signature)  # must not raise


def test_signature_accepts_valid_es256() -> None:
    auth = _authenticator(_EC_JWK, algorithms=("ES256",))
    token = jwt.encode({"sub": "x"}, _EC_PRIV, algorithm="ES256", headers={"kid": _EC_KID})
    header, _claims, signing_input, signature = _decode(token)
    auth._validate_signature(header, signing_input, signature)  # must not raise


# -- signature: reject ---------------------------------------------------------


def test_signature_rejects_alg_none() -> None:
    auth = _authenticator(_RSA_JWK)
    header = _b64url(json.dumps({"alg": "none", "kid": _RSA_KID}).encode())
    payload = _b64url(json.dumps({"sub": "x"}).encode())
    token = f"{header}.{payload}."  # unsigned "none" token
    h, _claims, signing_input, signature = _decode(token)
    _expect(
        AuthValidationError,
        "algorithm is not allowed",
        lambda: auth._validate_signature(h, signing_input, signature),
    )


def test_signature_rejects_disallowed_alg() -> None:
    auth = _authenticator(_RSA_JWK, algorithms=("RS256",))
    token = jwt.encode({"sub": "x"}, _RSA_PRIV, algorithm="RS384", headers={"kid": _RSA_KID})
    h, _claims, signing_input, signature = _decode(token)
    _expect(
        AuthValidationError,
        "algorithm is not allowed",
        lambda: auth._validate_signature(h, signing_input, signature),
    )


def test_signature_rejects_unknown_kid() -> None:
    auth = _authenticator(_RSA_JWK)
    token = jwt.encode({"sub": "x"}, _RSA_PRIV, algorithm="RS256", headers={"kid": "nope"})
    h, _claims, signing_input, signature = _decode(token)
    _expect(
        AuthValidationError,
        "key id not found",
        lambda: auth._validate_signature(h, signing_input, signature),
    )


def test_signature_requires_kid_when_jwks_has_multiple_keys() -> None:
    auth = _authenticator(_RSA_JWK, _RSA_JWK_OTHER)
    token = jwt.encode({"sub": "x"}, _RSA_PRIV, algorithm="RS256")  # no kid in header
    h, _claims, signing_input, signature = _decode(token)
    _expect(
        AuthValidationError,
        "key id is required",
        lambda: auth._validate_signature(h, signing_input, signature),
    )


def test_signature_rejects_token_signed_by_wrong_key() -> None:
    # JWKS holds _RSA_PRIV's public key under _RSA_KID, but the token is signed
    # by a different private key claiming the same kid.
    auth = _authenticator(_RSA_JWK)
    forged = jwt.encode({"sub": "x"}, _RSA_PRIV_OTHER, algorithm="RS256", headers={"kid": _RSA_KID})
    h, _claims, signing_input, signature = _decode(forged)
    _expect(
        AuthValidationError,
        "signature invalid",
        lambda: auth._validate_signature(h, signing_input, signature),
    )


def test_signature_rejects_tampered_payload() -> None:
    auth = _authenticator(_RSA_JWK)
    token = jwt.encode(
        {"sub": "x", "scope": "read"}, _RSA_PRIV, algorithm="RS256", headers={"kid": _RSA_KID}
    )
    head, _payload, sig = token.split(".")
    # Swap the payload (e.g. privilege escalation) but keep the original signature.
    tampered_payload = _b64url(json.dumps({"sub": "x", "scope": "admin"}).encode())
    tampered = f"{head}.{tampered_payload}.{sig}"
    h, _claims, signing_input, signature = _decode(tampered)
    _expect(
        AuthValidationError,
        "signature invalid",
        lambda: auth._validate_signature(h, signing_input, signature),
    )


# -- claims --------------------------------------------------------------------


def test_claims_rejects_expired() -> None:
    auth = _authenticator(_RSA_JWK)  # default clock_skew 60s
    now = int(time.time())
    _expect(AuthValidationError, "expired", lambda: auth._validate_claims({"exp": now - 200}))


def test_claims_allows_exp_within_skew() -> None:
    auth = _authenticator(_RSA_JWK)
    now = int(time.time())
    auth._validate_claims({"exp": now - 30})  # within 60s skew -> ok


def test_claims_requires_exp_in_non_dev() -> None:
    auth = _authenticator(_RSA_JWK)
    with _env(APP_ENV="prod"):
        _expect(AuthValidationError, "missing exp", lambda: auth._validate_claims({}))


def test_claims_allows_missing_exp_in_dev() -> None:
    auth = _authenticator(_RSA_JWK)
    with _env(APP_ENV="local"):
        auth._validate_claims({})  # ok


def test_claims_rejects_nbf_in_future() -> None:
    auth = _authenticator(_RSA_JWK)
    now = int(time.time())
    _expect(
        AuthValidationError,
        "not yet valid",
        lambda: auth._validate_claims({"exp": now + 3600, "nbf": now + 1000}),
    )


def test_claims_rejects_issuer_mismatch() -> None:
    auth = _authenticator(_RSA_JWK, issuer="https://good/")
    now = int(time.time())
    _expect(
        AuthValidationError,
        "issuer mismatch",
        lambda: auth._validate_claims({"exp": now + 3600, "iss": "https://evil/"}),
    )


def test_claims_rejects_audience_mismatch() -> None:
    auth = _authenticator(_RSA_JWK, audience="api://good")
    now = int(time.time())
    _expect(
        AuthValidationError,
        "audience mismatch",
        lambda: auth._validate_claims({"exp": now + 3600, "aud": "api://bad"}),
    )


def test_claims_rejects_tenant_mismatch() -> None:
    auth = _authenticator(_RSA_JWK, tenant="tenant-good")
    now = int(time.time())
    _expect(
        AuthValidationError,
        "tenant mismatch",
        lambda: auth._validate_claims({"exp": now + 3600, "tid": "tenant-bad"}),
    )


def test_claims_accepts_all_matching() -> None:
    auth = _authenticator(_RSA_JWK, issuer="https://good/", audience="api://good", tenant="t1")
    now = int(time.time())
    auth._validate_claims(
        {"exp": now + 3600, "iss": "https://good/", "aud": "api://good", "tid": "t1"}
    )


# -- validate_for_environment (non-dev hard-guards) ----------------------------


def test_env_guard_dev_mode_blocked_in_prod() -> None:
    with _env(APP_ENV="prod"):
        _expect(
            AuthConfigurationError,
            "AGENT_AUTH_MODE=dev",
            lambda: AuthConfig(mode="dev").validate_for_environment(),
        )


def test_env_guard_dev_mode_allowed_locally() -> None:
    with _env(APP_ENV="local"):
        AuthConfig(mode="dev").validate_for_environment()  # ok


def test_env_guard_jwt_prod_requires_jwks() -> None:
    with _env(APP_ENV="prod"):
        _expect(
            AuthConfigurationError,
            "JWKS configuration",
            lambda: AuthConfig(
                mode="jwt", issuer="i", audience="a", required_scopes=("s",)
            ).validate_for_environment(),
        )


def test_env_guard_jwt_prod_requires_issuer() -> None:
    with _env(APP_ENV="prod"):
        _expect(
            AuthConfigurationError,
            "issuer",
            lambda: AuthConfig(
                mode="jwt", jwks_url="https://idp/jwks", audience="a", required_scopes=("s",)
            ).validate_for_environment(),
        )


def test_env_guard_jwt_prod_requires_audience() -> None:
    with _env(APP_ENV="prod"):
        _expect(
            AuthConfigurationError,
            "audience",
            lambda: AuthConfig(
                mode="jwt", jwks_url="https://idp/jwks", issuer="i", required_scopes=("s",)
            ).validate_for_environment(),
        )


def test_env_guard_jwt_prod_requires_scopes() -> None:
    with _env(APP_ENV="prod"):
        _expect(
            AuthConfigurationError,
            "scope",
            lambda: AuthConfig(
                mode="jwt", jwks_url="https://idp/jwks", issuer="i", audience="a"
            ).validate_for_environment(),
        )


def test_env_guard_jwt_prod_requires_https_jwks() -> None:
    with _env(APP_ENV="prod"):
        _expect(
            AuthConfigurationError,
            "https",
            lambda: AuthConfig(
                mode="jwt",
                jwks_url="http://idp/jwks",
                issuer="i",
                audience="a",
                required_scopes=("s",),
            ).validate_for_environment(),
        )


def test_env_guard_valid_prod_config_passes() -> None:
    with _env(APP_ENV="prod"):
        AuthConfig(
            mode="jwt",
            jwks_url="https://idp/jwks",
            issuer="i",
            audience="a",
            required_scopes=("s",),
        ).validate_for_environment()  # ok


def test_env_guard_jwt_local_is_lenient() -> None:
    with _env(APP_ENV="local"):
        AuthConfig(mode="jwt").validate_for_environment()  # no non-dev guards apply


# -- JWKS cache ----------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args) -> bool:
        return False


def test_load_jwks_from_json_does_not_touch_network() -> None:
    auth = _authenticator(_RSA_JWK)
    jwks = auth._load_jwks()
    assert jwks["keys"][0]["kid"] == _RSA_KID


def test_load_jwks_url_is_fetched_once_and_cached() -> None:
    import urllib.request as urllib_request

    body = json.dumps({"keys": [_RSA_JWK]}).encode("utf-8")
    calls = {"n": 0}

    def fake_urlopen(url, timeout=None):
        calls["n"] += 1
        return _FakeResponse(body)

    config = AuthConfig(mode="jwt", jwks_url="https://idp/jwks", jwks_cache_seconds=300)
    auth = JwtAuthenticator(config)
    original = urllib_request.urlopen
    try:
        urllib_request.urlopen = fake_urlopen
        with _env(APP_ENV="local"):
            first = auth._load_jwks()
            second = auth._load_jwks()
        assert calls["n"] == 1  # second served from cache
        assert first["keys"][0]["kid"] == _RSA_KID
        assert second["keys"][0]["kid"] == _RSA_KID
    finally:
        urllib_request.urlopen = original


def test_load_jwks_url_refetches_after_expiry() -> None:
    import urllib.request as urllib_request

    body = json.dumps({"keys": [_RSA_JWK]}).encode("utf-8")
    calls = {"n": 0}

    def fake_urlopen(url, timeout=None):
        calls["n"] += 1
        return _FakeResponse(body)

    config = AuthConfig(mode="jwt", jwks_url="https://idp/jwks", jwks_cache_seconds=0)
    auth = JwtAuthenticator(config)
    original = urllib_request.urlopen
    try:
        urllib_request.urlopen = fake_urlopen
        with _env(APP_ENV="local"):
            auth._load_jwks()
            auth._load_jwks()
        assert calls["n"] == 2  # zero TTL -> always refetch
    finally:
        urllib_request.urlopen = original


def test_load_jwks_url_rejects_non_https_in_prod() -> None:
    config = AuthConfig(mode="jwt", jwks_url="http://idp/jwks", jwks_cache_seconds=300)
    auth = JwtAuthenticator(config)
    with _env(APP_ENV="prod"):
        _expect(AuthConfigurationError, "https", auth._load_jwks)


# -- end to end ----------------------------------------------------------------


def _e2e_config(required_scopes=("api.read",)) -> AuthConfig:
    return AuthConfig(
        mode="jwt",
        algorithms=("RS256",),
        jwks_json=json.dumps({"keys": [_RSA_JWK]}),
        issuer="https://idp/",
        audience="api://app",
        required_scopes=required_scopes,
    )


def test_authenticate_end_to_end_rs256() -> None:
    auth = JwtAuthenticator(_e2e_config())
    now = int(time.time())
    claims = {
        "iss": "https://idp/",
        "aud": "api://app",
        "exp": now + 3600,
        "tid": "tenant-1",
        "oid": "user-oid",
        "scp": "api.read api.write",
        "groups": ["g-1"],
    }
    token = jwt.encode(claims, _RSA_PRIV, algorithm="RS256", headers={"kid": _RSA_KID})
    with _env(APP_ENV="local", AGENT_AUTH_GRAPH_GROUPS_FALLBACK="false"):
        principal = auth.authenticate_authorization("Bearer " + token)
    assert principal.subject == "user-oid"
    assert principal.auth_mode == "jwt"
    assert "api.read" in principal.scopes
    assert principal.tenant == "tenant-1"
    assert principal.groups == ("g-1",)


def test_authenticate_rejects_missing_required_scope() -> None:
    auth = JwtAuthenticator(_e2e_config(required_scopes=("api.admin",)))
    now = int(time.time())
    claims = {
        "iss": "https://idp/",
        "aud": "api://app",
        "exp": now + 3600,
        "oid": "user-oid",
        "scp": "api.read",
        "groups": ["g-1"],
    }
    token = jwt.encode(claims, _RSA_PRIV, algorithm="RS256", headers={"kid": _RSA_KID})
    with _env(APP_ENV="local", AGENT_AUTH_GRAPH_GROUPS_FALLBACK="false"):
        _expect(
            AuthValidationError,
            "missing required scope",
            lambda: auth.authenticate_authorization("Bearer " + token),
        )


def test_authenticate_rejects_expired_token() -> None:
    auth = JwtAuthenticator(_e2e_config())
    now = int(time.time())
    claims = {
        "iss": "https://idp/",
        "aud": "api://app",
        "exp": now - 10_000,
        "oid": "user-oid",
        "scp": "api.read",
        "groups": ["g-1"],
    }
    token = jwt.encode(claims, _RSA_PRIV, algorithm="RS256", headers={"kid": _RSA_KID})
    with _env(APP_ENV="local", AGENT_AUTH_GRAPH_GROUPS_FALLBACK="false"):
        _expect(
            AuthValidationError,
            "expired",
            lambda: auth.authenticate_authorization("Bearer " + token),
        )


def test_authenticate_merges_token_groups_with_graph() -> None:
    """Token GUID groups are supplemented with Graph ids + display names."""
    import v1.utils.auth as auth_module
    from v1.utils.graph_groups import GraphGroup

    auth = JwtAuthenticator(_e2e_config())
    now = int(time.time())
    claims = {
        "iss": "https://idp/",
        "aud": "api://app",
        "exp": now + 3600,
        "oid": "user-oid",
        "scp": "api.read",
        "groups": ["guid-1"],
    }
    token = jwt.encode(claims, _RSA_PRIV, algorithm="RS256", headers={"kid": _RSA_KID})
    original = auth_module.resolve_groups_via_graph
    try:
        auth_module.resolve_groups_via_graph = lambda oid, **kw: (
            GraphGroup(id="guid-1", display_name="APPL-DEVELOPERS"),
            GraphGroup(id="guid-2", display_name=None),
        )
        with _env(APP_ENV="local", AGENT_AUTH_GRAPH_GROUPS_FALLBACK="true"):
            principal = auth.authenticate_authorization("Bearer " + token)
    finally:
        auth_module.resolve_groups_via_graph = original
    assert principal.groups == ("APPL-DEVELOPERS", "guid-1", "guid-2")


# -- bearer / decode hardening -------------------------------------------------


def test_extract_bearer_token_requires_scheme() -> None:
    _expect(AuthValidationError, "Missing Authorization", lambda: _extract_bearer_token(None))
    _expect(AuthValidationError, "Bearer scheme", lambda: _extract_bearer_token("Basic abc"))


def test_decode_rejects_non_jwt() -> None:
    _expect(AuthValidationError, "not a JWT", lambda: _decode_jwt("abc.def"))


def _main() -> int:
    checks = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for check in checks:
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - standalone runner reports all
            failures += 1
            print(f"FAIL {check.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {check.__name__}")
    print(f"\n{len(checks) - failures}/{len(checks)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())
