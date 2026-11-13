"""Minimal Etsy Open API v3 client: research (API key only) and listing management (OAuth).

Read endpoints (search, listing stats, taxonomy) need only ETSY_API_KEY.
Write endpoints (create listing, upload files, receipts) need an OAuth token from `python -m ultron etsy-auth`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlencode

import requests

API = "https://api.etsy.com/v3"
SCOPES = "listings_r listings_w transactions_r shops_r"


class EtsyError(RuntimeError):
    pass


class EtsyAPI:
    def __init__(self, ctx):
        self.key = ctx.env("ETSY_API_KEY")
        self.redirect_uri = ctx.env("ETSY_REDIRECT_URI") or "http://localhost:3003/callback"
        self.token_path: Path = ctx.root / "data" / "etsy_token.json"
        self._shop_id = ctx.env("ETSY_SHOP_ID")

    # --- capability checks -----------------------------------------------------

    @property
    def can_read(self) -> bool:
        return bool(self.key)

    @property
    def can_write(self) -> bool:
        return bool(self.key and self.token_path.exists() and self.shop_id)

    @property
    def keystring(self) -> str:
        return (self.key or "").split(":")[0]

    @property
    def shop_id(self) -> str | None:
        if self._shop_id:
            return self._shop_id
        if self.token_path.exists():
            return json.loads(self.token_path.read_text()).get("shop_id")
        return None

    # --- OAuth 2.0 with PKCE ---------------------------------------------------

    def auth_url(self) -> tuple[str, str, str]:
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        state = secrets.token_urlsafe(16)
        query = urlencode({
            "response_type": "code", "redirect_uri": self.redirect_uri, "scope": SCOPES,
            "client_id": self.keystring, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        return f"https://www.etsy.com/oauth/connect?{query}", verifier, state

    def exchange_code(self, code: str, verifier: str) -> dict:
        resp = requests.post(f"{API}/public/oauth/token", data={
            "grant_type": "authorization_code", "client_id": self.keystring,
            "redirect_uri": self.redirect_uri, "code": code, "code_verifier": verifier,
        }, timeout=30)
        self._raise(resp)
        token = self._store_token(resp.json())
        me = self._get("/application/users/me", auth=True)
        token["shop_id"] = str(me.get("shop_id") or "")
        self.token_path.write_text(json.dumps(token, indent=2))
        return token

    def _store_token(self, data: dict, keep: dict | None = None) -> dict:
        token = dict(keep or {})
        token.update(access_token=data["access_token"], refresh_token=data["refresh_token"],
                     expires_at=time.time() + int(data.get("expires_in", 3600)))
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(token, indent=2))
        return token

    def _access_token(self) -> str:
        if not self.token_path.exists():
            raise EtsyError("No Etsy OAuth token. Run `python -m ultron etsy-auth` first.")
        token = json.loads(self.token_path.read_text())
        if token["expires_at"] - time.time() < 120:
            resp = requests.post(f"{API}/public/oauth/token", data={
                "grant_type": "refresh_token", "client_id": self.keystring,
                "refresh_token": token["refresh_token"],
            }, timeout=30)
            self._raise(resp)
            token = self._store_token(resp.json(), keep=token)
        return token["access_token"]

    # --- HTTP helpers ------------------------------------------------------------

    def _headers(self, auth: bool) -> dict:
        if not self.key:
            raise EtsyError("ETSY_API_KEY is not set")
        headers = {"x-api-key": self.key}
        if auth:
            headers["Authorization"] = f"Bearer {self._access_token()}"
        return headers

    @staticmethod
    def _raise(resp: requests.Response) -> None:
        if resp.status_code >= 400:
            raise EtsyError(f"Etsy API {resp.status_code}: {resp.text[:500]}")

    def _get(self, path: str, auth: bool = False, **params) -> dict:
        resp = requests.get(API + path, headers=self._headers(auth), params=params, timeout=30)
        self._raise(resp)
        return resp.json()

    # --- research ------------------------------------------------------------------

    def search_active(self, keywords: str, limit: int = 25) -> dict:
        """Returns {'count': total matching listings, 'results': [...top listings...]}."""
        return self._get("/application/listings/active", keywords=keywords, limit=limit)

    def get_listing(self, listing_id: str) -> dict:
        return self._get(f"/application/listings/{listing_id}")

    def taxonomy_nodes(self) -> list[dict]:
        def flatten(nodes, trail=""):
            for node in nodes:
                path = f"{trail} > {node['name']}" if trail else node["name"]
                yield {"id": node["id"], "path": path}
                yield from flatten(node.get("children") or [], path)
        return list(flatten(self._get("/application/seller-taxonomy/nodes")["results"]))

    # --- selling (OAuth) ---------------------------------------------------------

    def create_draft_listing(self, *, title: str, description: str, price: float, tags: list[str],
                             taxonomy_id: int) -> dict:
        body = {
            "quantity": 999, "title": title, "description": description, "price": round(price, 2),
            "who_made": "i_did", "when_made": "made_to_order", "taxonomy_id": taxonomy_id,
            "type": "download", "is_supply": False, "tags": tags,
        }
        resp = requests.post(f"{API}/application/shops/{self.shop_id}/listings",
                             headers=self._headers(True), json=body, timeout=60)
        self._raise(resp)
        return resp.json()

    # Etsy's upload endpoints sometimes return a generic 400 "please try again"; those are retried.
    _RETRYABLE = ("please try again", "temporarily", "timeout")

    def _upload(self, url: str, field: str, path: Path, mime: str, data: dict | None = None,
                attempts: int = 4) -> dict:
        name = (data or {}).get("name") or path.name
        blob = path.read_bytes()
        last = None
        for attempt in range(attempts):
            resp = requests.post(url, headers=self._headers(True), data=data or {},
                                 files={field: (name, blob, mime)}, timeout=120)
            retryable = resp.status_code >= 500 or (
                resp.status_code in (400, 409, 429) and any(t in resp.text.lower() for t in self._RETRYABLE))
            if resp.status_code < 400 or not retryable:
                self._raise(resp)
                return resp.json()
            last = resp
            if attempt < attempts - 1:
                time.sleep(3 * (attempt + 1) ** 2)   # 3s, 12s, 27s
        self._raise(last)
        return {}

    @staticmethod
    def safe_file_name(path: Path) -> str:
        """Plain ASCII name Etsy accepts for a download file (letters, digits, dash, dot; <= 70 chars)."""
        ascii_stem = unicodedata.normalize("NFKD", path.stem).encode("ascii", "ignore").decode()
        stem = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in ascii_stem)
        stem = "-".join(part for part in stem.split("-") if part)[:60] or "printable"
        return f"{stem}{path.suffix.lower()}"

    def upload_image(self, listing_id: str, path: Path) -> dict:
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return self._upload(f"{API}/application/shops/{self.shop_id}/listings/{listing_id}/images",
                            "image", path, mime)

    def upload_file(self, listing_id: str, path: Path) -> dict:
        mime = "application/pdf" if path.suffix.lower() == ".pdf" else "application/octet-stream"
        return self._upload(f"{API}/application/shops/{self.shop_id}/listings/{listing_id}/files",
                            "file", path, mime, data={"name": self.safe_file_name(path), "rank": "1"})

    def listing_images(self, listing_id: str) -> list[dict]:
        return self._get(f"/application/listings/{listing_id}/images").get("results", [])

    def listing_files(self, listing_id: str) -> list[dict]:
        return self._get(f"/application/shops/{self.shop_id}/listings/{listing_id}/files", auth=True).get("results", [])

    def find_draft(self, title: str) -> str | None:
        """Listing id of a draft in the shop with exactly this title (to recover a half-finished publish)."""
        page = self._get(f"/application/shops/{self.shop_id}/listings", auth=True, state="draft", limit=100)
        for item in page.get("results", []):
            if (item.get("title") or "").strip() == title.strip():
                return str(item["listing_id"])
        return None

    def update_listing(self, listing_id: str, **fields) -> dict:
        resp = requests.patch(f"{API}/application/shops/{self.shop_id}/listings/{listing_id}",
                              headers=self._headers(True), json=fields, timeout=60)
        self._raise(resp)
        return resp.json()

    def receipts(self, min_created: int) -> list[dict]:
        out, offset = [], 0
        while True:
            page = self._get(f"/application/shops/{self.shop_id}/receipts", auth=True,
                             min_created=min_created, limit=100, offset=offset)
            out.extend(page.get("results", []))
            offset += 100
            if offset >= page.get("count", 0):
                return out
