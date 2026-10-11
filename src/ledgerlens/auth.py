"""Networkless verification of Clerk session JWTs at the HTTP boundary."""

from __future__ import annotations

import jwt
from fastapi import Request

from ledgerlens.settings import Settings


def verified_reviewer(request: Request) -> str | None:
    settings: Settings = request.app.state.settings
    if not (
        settings.clerk_jwt_key
        and settings.clerk_issuer
        and settings.clerk_authorized_parties
    ):
        return None
    authorization = request.headers.get("authorization")
    token = request.cookies.get("__session")
    if authorization is not None:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return None
    if not token:
        return None
    if (
        authorization is None
        and request.method not in {"GET", "HEAD", "OPTIONS"}
        and request.headers.get("origin") not in settings.clerk_authorized_parties
    ):
        return None
    try:
        claims = jwt.decode(
            token,
            settings.clerk_jwt_key,
            algorithms=["RS256"],
            issuer=settings.clerk_issuer,
            options={"require": ["exp", "nbf", "iat", "iss", "sub", "sid", "azp"]},
        )
    except (jwt.PyJWTError, ValueError):
        return None
    if (
        claims["azp"] not in settings.clerk_authorized_parties
        or not isinstance(claims["sub"], str)
        or not claims["sub"]
        or not isinstance(claims["sid"], str)
        or not claims["sid"]
        or claims.get("sts") == "pending"
    ):
        return None
    return str(claims["sub"])
