"""Minimal Pinterest API v5 client: create a pin from a local image."""
from __future__ import annotations

import base64
from pathlib import Path

import requests

API = "https://api.pinterest.com/v5"


class PinterestAPI:
    def __init__(self, ctx):
        self.token = ctx.env("PINTEREST_ACCESS_TOKEN")
        self.board_id = ctx.env("PINTEREST_BOARD_ID")

    @property
    def can_post(self) -> bool:
        return bool(self.token and self.board_id)

    def create_pin(self, *, title: str, description: str, link: str, alt_text: str, image: Path) -> dict:
        body = {
            "board_id": self.board_id, "title": title, "description": description,
            "link": link, "alt_text": alt_text,
            "media_source": {"source_type": "image_base64", "content_type": "image/png",
                             "data": base64.b64encode(image.read_bytes()).decode()},
        }
        resp = requests.post(f"{API}/pins", json=body, timeout=60,
                             headers={"Authorization": f"Bearer {self.token}"})
        if resp.status_code >= 400:
            raise RuntimeError(f"Pinterest API {resp.status_code}: {resp.text[:500]}")
        return resp.json()
