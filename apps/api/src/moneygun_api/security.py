from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AuthenticationError(RuntimeError):
    pass


class OidcBearerVerifier:
    """Production-only OIDC verifier; broker approval remains a separate owner factor."""

    def __init__(self) -> None:
        self.issuer = os.getenv("MONEYGUN_OIDC_ISSUER", "").strip().rstrip("/")
        self.client_id = os.getenv("MONEYGUN_OIDC_CLIENT_ID", "").strip()
        self._jwk_client: Any | None = None

    @property
    def configured(self) -> bool:
        return self.issuer.startswith("https://") and bool(self.client_id)

    def _discovery(self) -> dict[str, Any]:
        if not self.configured:
            raise AuthenticationError("운영 OIDC 발급자와 클라이언트 ID가 설정되지 않았습니다.")
        request = Request(
            f"{self.issuer}/.well-known/openid-configuration",
            headers={"Accept": "application/json", "User-Agent": "MoneyGun/0.8"},
        )
        try:
            with urlopen(request, timeout=5) as response:  # noqa: S310 - HTTPS issuer enforced
                payload = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise AuthenticationError("OIDC 메타데이터를 확인하지 못했습니다.") from error
        if payload.get("issuer", "").rstrip("/") != self.issuer:
            raise AuthenticationError("OIDC 메타데이터의 발급자가 설정값과 다릅니다.")
        if not str(payload.get("jwks_uri", "")).startswith("https://"):
            raise AuthenticationError("OIDC JWKS 주소가 안전한 HTTPS가 아닙니다.")
        return payload

    def verify(self, token: str) -> dict[str, Any]:
        try:
            import jwt
        except ImportError as error:  # pragma: no cover - production dependency check
            raise AuthenticationError("OIDC 검증 패키지가 설치되지 않았습니다.") from error
        try:
            if self._jwk_client is None:
                metadata = self._discovery()
                self._jwk_client = jwt.PyJWKClient(metadata["jwks_uri"], cache_keys=True)
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"],
                audience=self.client_id,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
        except Exception as error:
            raise AuthenticationError("로그인 토큰이 유효하지 않습니다.") from error
        return dict(claims)
