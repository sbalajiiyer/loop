"""Small OAuth 2.0 authorization-code helper shared by the WHOOP and Oura sources.
Tokens live in the local database and refresh themselves."""
from __future__ import annotations

import secrets
import time
import urllib.parse
from dataclasses import dataclass

import httpx

from .. import config, db


@dataclass
class Provider:
    name: str              # 'whoop' | 'oura'
    authorize_url: str
    token_url: str
    scopes: str
    client_id: str
    client_secret: str

    @property
    def redirect_uri(self) -> str:
        return f"{config.BASE_URL}/auth/{self.name}/callback"

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def connected(self) -> bool:
        return bool(db.kv_get(f"token:{self.name}"))

    def start(self) -> str:
        state = secrets.token_urlsafe(16)
        db.kv_set(f"oauth_state:{self.name}", state)
        q = urllib.parse.urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri,
            "response_type": "code", "scope": self.scopes, "state": state})
        return f"{self.authorize_url}?{q}"

    def _save(self, tok: dict) -> None:
        old = db.kv_get(f"token:{self.name}") or {}
        tok.setdefault("refresh_token", old.get("refresh_token"))
        tok["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600)) - 60
        db.kv_set(f"token:{self.name}", tok)

    def finish(self, code: str, state: str) -> None:
        if not state or state != db.kv_get(f"oauth_state:{self.name}"):
            raise ValueError("OAuth state mismatch; start the connection again")
        db.kv_del(f"oauth_state:{self.name}")
        r = httpx.post(self.token_url, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "client_secret": self.client_secret}, timeout=30)
        r.raise_for_status()
        self._save(r.json())

    def access_token(self) -> str:
        tok = db.kv_get(f"token:{self.name}")
        if not tok:
            raise RuntimeError(f"{self.name} is not connected")
        if time.time() >= tok.get("expires_at", 0):
            data = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                    "client_id": self.client_id, "client_secret": self.client_secret}
            if self.name == "whoop":
                data["scope"] = "offline"
            r = httpx.post(self.token_url, data=data, timeout=30)
            r.raise_for_status()
            self._save(r.json())
            tok = db.kv_get(f"token:{self.name}")
        return tok["access_token"]

    def disconnect(self) -> None:
        db.kv_del(f"token:{self.name}")

    def get(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(4):
            r = httpx.get(url, params=params, timeout=30,
                          headers={"Authorization": f"Bearer {self.access_token()}"})
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}
