"""Hosted HTTP authentication uses a verified Clerk session, never a header identity."""

from fastapi.testclient import TestClient

from ledgerlens.app import create_app
from ledgerlens.settings import Settings


def test_forged_reviewer_header_is_not_a_session(pg_dsns: dict[str, str]) -> None:
    app = create_app(
        Settings(
            hosted=True,
            intake_dsn=pg_dsns["intake"],
            read_dsn=pg_dsns["read"],
            audit_dsn=pg_dsns["audit"],
        )
    )
    with TestClient(app) as client:
        response = client.get(
            "/", headers={"Accept": "application/json", "X-Reviewer": "victim"}
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "sign-in required"}


def test_only_valid_session_claims_can_open_a_workspace(
    pg_dsns: dict[str, str],
) -> None:
    import time

    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    app = create_app(
        Settings(
            hosted=True,
            intake_dsn=pg_dsns["intake"],
            read_dsn=pg_dsns["read"],
            audit_dsn=pg_dsns["audit"],
            clerk_issuer="https://test.clerk.accounts.dev",
            clerk_jwt_key=public,
            clerk_authorized_parties=["https://demo.example.com"],
        )
    )
    claims = {
        "sub": "user_signed",
        "sid": "sess_test",
        "iss": "https://test.clerk.accounts.dev",
        "azp": "https://demo.example.com",
        "exp": int(time.time()) + 60,
        "nbf": int(time.time()) - 5,
        "iat": int(time.time()) - 5,
    }
    with TestClient(app) as client:
        for changes in (
            {"exp": 1},
            {"nbf": int(time.time()) + 600},
            {"iss": "https://attacker.example"},
            {"azp": "https://attacker.example"},
            {"sub": ""},
            {"sid": ""},
            {"sts": "pending"},
        ):
            token = jwt.encode({**claims, **changes}, key, algorithm="RS256")
            assert (
                client.get(
                    "/",
                    headers={
                        "Accept": "application/json",
                        "Authorization": "Bearer " + token,
                    },
                ).status_code
                == 401
            )
        forged = jwt.encode(
            claims,
            rsa.generate_private_key(public_exponent=65537, key_size=2048),
            algorithm="RS256",
        )
        assert (
            client.get(
                "/",
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer " + forged,
                },
            ).status_code
            == 401
        )
        token = jwt.encode(claims, key, algorithm="RS256")
        response = client.get(
            "/",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + token,
                "X-Reviewer": "attacker",
            },
        )
        assert response.status_code == 200
        client.cookies.set("__session", token)
        assert (
            client.get("/", headers={"Accept": "application/json"}).json()
            == response.json()
        )
        assert (
            client.post(
                "/demo-attempts/start-fresh",
                headers={
                    "Accept": "application/json",
                    "Origin": "https://evil.example.com",
                },
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/demo-attempts/start-fresh",
                headers={
                    "Accept": "application/json",
                    "Origin": "https://demo.example.com",
                },
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/demo-attempts/start-fresh", headers={"Accept": "application/json"}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/demo-attempts/start-fresh",
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer " + token,
                },
            ).status_code
            == 200
        )


def test_unsigned_browser_can_enter_clerk_sign_in(pg_dsns: dict[str, str]) -> None:
    app = create_app(
        Settings(
            hosted=True,
            intake_dsn=pg_dsns["intake"],
            read_dsn=pg_dsns["read"],
            audit_dsn=pg_dsns["audit"],
            clerk_issuer="https://test.clerk.accounts.dev",
            clerk_publishable_key="pk_test_public",
        )
    )
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 401
        assert 'id="sign-in"' in page.text
        assert "mountSignIn" in page.text
        assert "withSignUp: false" in page.text
        assert 'action="/ingest"' not in page.text
