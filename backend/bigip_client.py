"""iControl REST client for BIG-IP management APIs."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urljoin

import requests
import urllib3


class BigIPError(Exception):
    pass


class BigIPClient:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        verify_tls: bool = False,
        timeout: int = 60,
    ) -> None:
        host = host.strip().rstrip("/")
        if host.startswith("http://") or host.startswith("https://"):
            self.base = host
        else:
            self.base = f"https://{host}"
        self.username = username
        self.password = password
        self.verify_tls = verify_tls
        self.timeout = timeout
        self._token: str | None = None
        self._session = requests.Session()
        if not verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _url(self, path: str) -> str:
        path = path if path.startswith("/") else f"/{path}"
        return urljoin(self.base + "/", path.lstrip("/"))

    def _token_url(self) -> str:
        if not self._token:
            raise BigIPError("Not authenticated")
        safe = quote(str(self._token), safe="")
        return self._url(f"/mgmt/shared/authz/tokens/{safe}")

    @staticmethod
    def _extract_token(data: Any) -> str:
        if not isinstance(data, dict):
            raise BigIPError("Login response is not a JSON object")
        token_field = data.get("token")
        if isinstance(token_field, str) and token_field.strip():
            return token_field.strip()
        if isinstance(token_field, dict):
            for key in ("token", "name", "authToken"):
                value = token_field.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        # Some versions nest differently
        for key in ("token", "authToken"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        raise BigIPError(
            "Login response missing token "
            f"(keys: {', '.join(sorted(data.keys())[:12])})",
        )

    def login(self) -> None:
        url = self._url("/mgmt/shared/authn/login")
        payloads = [
            {
                "username": self.username,
                "password": self.password,
                "loginProviderName": "tmos",
            },
            {"username": self.username, "password": self.password},
        ]
        last_error = "Login failed"
        for payload in payloads:
            try:
                r = self._session.post(
                    url,
                    json=payload,
                    verify=self.verify_tls,
                    timeout=self.timeout,
                    headers={"Content-Type": "application/json"},
                )
            except requests.RequestException as exc:
                raise BigIPError(f"Cannot reach BIG-IP at {self.base}: {exc}") from exc
            if r.status_code >= 400:
                last_error = f"Login failed ({r.status_code}): {r.text[:400]}"
                continue
            try:
                data = r.json()
            except json.JSONDecodeError as exc:
                raise BigIPError("Login returned non-JSON response") from exc
            self._token = self._extract_token(data)
            self._session.headers["X-F5-Auth-Token"] = self._token
            return
        raise BigIPError(last_error)

    def extend_token(self, *, timeout: int = 3600) -> None:
        """Extend token lifetime (default login timeout is 1200s)."""
        if not self._token:
            return
        url = self._token_url()
        body: dict[str, int | str] = {"timeout": min(timeout, 36000)}
        try:
            r = self._session.patch(
                url,
                json=body,
                verify=self.verify_tls,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
            )
        except requests.RequestException as exc:
            raise BigIPError(f"Token extension request failed: {exc}") from exc
        if r.status_code == 406:
            # Max allowed timeout exceeded — retry at platform maximum.
            body = {"timeout": 36000}
            try:
                r = self._session.patch(
                    url,
                    json=body,
                    verify=self.verify_tls,
                    timeout=self.timeout,
                    headers={"Content-Type": "application/json"},
                )
            except requests.RequestException as exc:
                raise BigIPError(f"Token extension request failed: {exc}") from exc
        if r.status_code >= 400:
            detail = r.text.replace("\n", " ").strip()[:400]
            raise BigIPError(
                f"Token extension failed ({r.status_code}): {detail or 'no response body'}",
            )

    def get(self, endpoint: str, *, params: dict[str, Any] | None = None) -> Any:
        if not self._token:
            self.login()
        url = self._url(endpoint)
        try:
            r = self._session.get(
                url,
                params=params,
                verify=self.verify_tls,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise BigIPError(f"GET {endpoint} failed: {exc}") from exc
        if r.status_code == 401:
            self.login()
            try:
                r = self._session.get(
                    url,
                    params=params,
                    verify=self.verify_tls,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise BigIPError(f"GET {endpoint} failed: {exc}") from exc
        if r.status_code >= 400:
            raise BigIPError(f"GET {endpoint} failed ({r.status_code}): {r.text[:400]}")
        if not r.text.strip():
            return {}
        try:
            return r.json()
        except json.JSONDecodeError as exc:
            raise BigIPError(f"Non-JSON response from {endpoint}") from exc

    def logout(self) -> None:
        if not self._token:
            return
        try:
            self._session.delete(
                self._token_url(),
                verify=self.verify_tls,
                timeout=10,
            )
        except requests.RequestException:
            pass
        finally:
            self._token = None
            self._session.headers.pop("X-F5-Auth-Token", None)
