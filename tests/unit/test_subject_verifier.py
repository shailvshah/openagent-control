"""Subject-token verification (ADR-0019), against a real local JWKS server.

Same real-HTTP pattern as test_oidc_jwks.py and test_operator_identity.py:
PyJWKClient fetches keys over real HTTP internally, so a MockTransport cannot
intercept it and a fake would only prove this project agrees with itself.
"""

from __future__ import annotations

import datetime
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from openagent_control.adapters.identity.oidc_subject import OidcSubjectVerifier
from openagent_control.application.governed_execution import _bind_subject
from openagent_control.domain.errors import IdentityError
from openagent_control.domain.models import AgentIdentity, SubjectIdentity

_ISSUER_PATH = "/.well-known/openid-configuration"
_JWKS_PATH = "/keys"
_AUDIENCE = "api://openagent-control-gateway"
_KID = "subject-key-1"


class _Idp:
    def __init__(self, base_url: str) -> None:
        self.issuer = base_url
        self.discovery_url = base_url + _ISSUER_PATH


@pytest.fixture(scope="module")
def idp() -> Iterator[tuple[_Idp, rsa.RSAPrivateKey]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": _KID, "use": "sig", "alg": "RS256"})
    jwks_body = json.dumps({"keys": [jwk]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            base = f"http://{self.headers['Host']}"
            if self.path == _ISSUER_PATH:
                body = json.dumps({"issuer": base, "jwks_uri": base + _JWKS_PATH}).encode()
            elif self.path == _JWKS_PATH:
                body = jwks_body
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Idp(f"http://127.0.0.1:{server.server_port}"), private_key
    finally:
        server.shutdown()
        thread.join()


def _token(
    key: rsa.RSAPrivateKey, idp: _Idp, claims: dict[str, Any], audience: str = _AUDIENCE
) -> str:
    payload = {
        "iss": idp.issuer,
        "aud": audience,
        "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=300),
        **claims,
    }
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": _KID})


def _verifier(idp: _Idp, role_claim: str = "roles") -> OidcSubjectVerifier:
    return OidcSubjectVerifier(idp.discovery_url, audience=_AUDIENCE, role_claim=role_claim)


# --- verification ----------------------------------------------------------


@pytest.mark.asyncio
async def test_subject_id_is_issuer_scoped_never_a_bare_sub(
    idp: tuple[_Idp, rsa.RSAPrivateKey],
) -> None:
    """OIDC Core §5.7: `sub` is unique only within an issuer, so the identifier
    must be the iss/sub pair or it collides across federated issuers."""
    server, key = idp
    token = _token(key, server, {"sub": "a1b2c3"})

    subject = await _verifier(server).verify(token)

    assert subject.subject_id == f"{server.issuer}#a1b2c3"
    assert subject.issuer == server.issuer


@pytest.mark.asyncio
async def test_roles_and_scopes_are_projected_for_policy(
    idp: tuple[_Idp, rsa.RSAPrivateKey],
) -> None:
    server, key = idp
    token = _token(
        key,
        server,
        {"sub": "a1b2c3", "roles": ["finance-approver"], "scope": "invoices:read invoices:write"},
    )

    subject = await _verifier(server).verify(token)

    assert subject.roles == ["finance-approver"]
    assert subject.scopes == ["invoices:read", "invoices:write"]


@pytest.mark.asyncio
async def test_keycloak_nested_role_claim_is_resolved(
    idp: tuple[_Idp, rsa.RSAPrivateKey],
) -> None:
    """Keycloak nests realm roles; roles are not a standard OIDC claim, so the
    path is configurable rather than assumed."""
    server, key = idp
    token = _token(key, server, {"sub": "a1b2c3", "realm_access": {"roles": ["finance-approver"]}})

    subject = await _verifier(server, role_claim="realm_access.roles").verify(token)

    assert subject.roles == ["finance-approver"]


@pytest.mark.asyncio
async def test_username_is_captured_but_is_not_the_identifier(
    idp: tuple[_Idp, rsa.RSAPrivateKey],
) -> None:
    """OIDC Core §5.7 says preferred_username/email MUST NOT identify a user —
    they are mutable and reassignable. It is display-only here."""
    server, key = idp
    token = _token(key, server, {"sub": "a1b2c3", "preferred_username": "dana@corp.net"})

    subject = await _verifier(server).verify(token)

    assert subject.username == "dana@corp.net"
    assert subject.subject_id == f"{server.issuer}#a1b2c3"


@pytest.mark.asyncio
async def test_an_id_token_is_rejected_as_a_subject_token(
    idp: tuple[_Idp, rsa.RSAPrivateKey],
) -> None:
    """An ID token must never authorize an API call. No special case is needed
    — its `aud` is the client id, so validating against this resource's
    audience rejects it. Asserted so the property stays deliberate."""
    server, key = idp
    id_token = _token(key, server, {"sub": "a1b2c3", "nonce": "n-1"}, audience="some-client-id")

    with pytest.raises(IdentityError, match="invalid subject token"):
        await _verifier(server).verify(id_token)


@pytest.mark.asyncio
async def test_an_expired_subject_token_is_rejected(
    idp: tuple[_Idp, rsa.RSAPrivateKey],
) -> None:
    server, key = idp
    payload = {
        "iss": server.issuer,
        "aud": _AUDIENCE,
        "sub": "a1b2c3",
        "exp": datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=10),
    }
    expired = jwt.encode(payload, key, algorithm="RS256", headers={"kid": _KID})

    with pytest.raises(IdentityError, match="invalid subject token"):
        await _verifier(server).verify(expired)


@pytest.mark.asyncio
async def test_a_token_signed_by_a_stranger_is_rejected(
    idp: tuple[_Idp, rsa.RSAPrivateKey],
) -> None:
    server, _key = idp
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = _token(other_key, server, {"sub": "a1b2c3"})

    with pytest.raises(IdentityError, match="invalid subject token"):
        await _verifier(server).verify(forged)


@pytest.mark.asyncio
async def test_a_missing_subject_token_is_rejected(idp: tuple[_Idp, rsa.RSAPrivateKey]) -> None:
    server, _key = idp

    with pytest.raises(IdentityError, match="missing a subject token"):
        await _verifier(server).verify("")


@pytest.mark.asyncio
async def test_may_act_is_extracted_when_the_idp_issues_it(
    idp: tuple[_Idp, rsa.RSAPrivateKey],
) -> None:
    server, key = idp
    token = _token(key, server, {"sub": "a1b2c3", "may_act": {"sub": "invoice-bot"}})

    subject = await _verifier(server).verify(token)

    assert subject.authorized_actor == "invoice-bot"


# --- binding ---------------------------------------------------------------


def _subject(subject_id: str = "iss#dana", actor: str | None = None) -> SubjectIdentity:
    return SubjectIdentity(subject_id=subject_id, issuer="iss", authorized_actor=actor)


def _agent(sponsor: str | None = "iss#dana", client_id: str = "invoice-bot") -> AgentIdentity:
    return AgentIdentity(
        spiffe_id="oidc://iss/invoice-bot", human_sponsor=sponsor, client_id=client_id
    )


def test_may_act_naming_this_agent_binds_the_call() -> None:
    _bind_subject(_agent(), _subject(actor="invoice-bot"), "strict")


def test_may_act_naming_a_different_agent_is_refused() -> None:
    """The user authorized some other workload to act for them, not this one."""
    with pytest.raises(IdentityError, match="may_act mismatch"):
        _bind_subject(_agent(), _subject(actor="other-bot"), "strict")


def test_a_subject_token_for_a_different_user_is_refused() -> None:
    """Without this, an agent holding any user's valid token could present it
    while claiming a different sponsor: the IdP mints a real credential for
    that user and the receipt attributes the call to someone else."""
    with pytest.raises(IdentityError, match="does not belong to the sponsor"):
        _bind_subject(_agent(sponsor="iss#dana"), _subject(subject_id="iss#erin"), "strict")


def test_the_mismatch_error_names_the_pairwise_escape_hatch() -> None:
    """A pairwise-subject tenant hits this legitimately, so the error has to
    say what to do rather than just refusing."""
    with pytest.raises(IdentityError, match="may-act-only"):
        _bind_subject(_agent(sponsor="iss#dana"), _subject(subject_id="iss#erin"), "strict")


def test_may_act_only_mode_skips_the_equality_fallback() -> None:
    """Pairwise subject identifiers make the same human's `sub` differ between
    the agent's token and the subject token, so equality would reject a valid
    delegated call."""
    _bind_subject(
        _agent(sponsor="iss#dana"), _subject(subject_id="iss#pairwise-xyz"), "may-act-only"
    )


def test_may_act_still_binds_in_may_act_only_mode() -> None:
    with pytest.raises(IdentityError, match="may_act mismatch"):
        _bind_subject(_agent(), _subject(actor="other-bot"), "may-act-only")


def test_binding_off_accepts_any_verified_subject() -> None:
    _bind_subject(_agent(sponsor="iss#dana"), _subject(subject_id="iss#erin"), "off")
