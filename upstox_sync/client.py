"""Upstox v2 client — Fundamentals + Options + FII/DII + Market.

All paths verified live against api.upstox.com/v2:

  Fundamentals (by ISIN):
    /fundamentals/{isin}/{profile,income-statement,balance-sheet,cash-flow,
                          key-ratios,share-holdings,corporate-actions}
  Fundamentals (by instrument_key):
    /fundamentals/{instrument_key}/competitors
  Options (instrument_key + expiry keyword like current_month + date + bucket_interval):
    /market/pcr, /market/max-pain, /market/oi
  FII / DII (data_type segments + interval 1D|1M):
    /market/fii, /market/dii
  Market:
    /market/holidays, /market/status/{exchange}

Tokens expire daily ~03:30 IST; a 401 raises TokenExpiredError.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Any

import requests

BASE_URL = "https://api.upstox.com/v2"
DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 5
DEFAULT_BACKOFF = 1.0
MAX_BACKOFF = 20.0
RATE_LIMIT_DELAY = 0.28


class UpstoxError(Exception):
    """Any non-recoverable Upstox failure."""


class TokenExpiredError(UpstoxError):
    """401 — token expired/invalid (rotates daily ~03:30 IST)."""


class NotEntitledError(UpstoxError):
    """403 — this API family is not enabled for the account."""


class UpstoxClient:
    def __init__(self, token: str, *, timeout: float = DEFAULT_TIMEOUT,
                 retries: int = DEFAULT_RETRIES, backoff: float = DEFAULT_BACKOFF) -> None:
        if not token or token in ("PASTE_YOUR_TOKEN_HERE", "your_token_here"):
            raise UpstoxError("Missing Upstox access token (run `login` first)")
        self._s = requests.Session()
        self._s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self._timeout, self._retries, self._backoff = timeout, retries, backoff

    def close(self) -> None:
        self._s.close()

    def __enter__(self) -> "UpstoxClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(self, path: str, params: Any = None) -> dict:
        url = f"{BASE_URL}{path}"
        last = "no attempts"
        for attempt in range(self._retries):
            try:
                time.sleep(RATE_LIMIT_DELAY)
                r = self._s.get(url, params=params, timeout=self._timeout)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 401:
                    raise TokenExpiredError("Upstox 401: token expired/invalid (refresh daily).")
                if r.status_code == 403:
                    raise NotEntitledError(f"Upstox 403 on {path}: family not entitled.")
                if r.status_code == 429 or r.status_code >= 500:
                    ra = r.headers.get("Retry-After")
                    wait = float(ra) if (ra and ra.isdigit()) else min(self._backoff * (2 ** attempt), MAX_BACKOFF)
                    last = f"HTTP {r.status_code} (waited {wait:.1f}s)"
                    time.sleep(wait)
                    continue
                # 400/404 etc: return the parsed error so callers can decide (e.g. no options)
                try:
                    return r.json()
                except Exception:
                    raise UpstoxError(f"HTTP {r.status_code} on {path}: {r.text[:160]}")
            except (requests.ConnectionError, requests.Timeout) as e:
                last = repr(e)
                time.sleep(min(self._backoff * (2 ** attempt), MAX_BACKOFF))
        raise UpstoxError(f"Failed after {self._retries} retries on {path}: {last}")

    @staticmethod
    def _q(key: str) -> str:
        return urllib.parse.quote(key, safe="")

    # -------------------- Fundamentals (by ISIN) -------------------- #
    def profile(self, isin: str) -> dict:
        return self._get(f"/fundamentals/{isin}/profile").get("data", {}) or {}

    def income_statement(self, isin: str, interval: str = "annual") -> dict:
        return self._get(f"/fundamentals/{isin}/income-statement", {"interval": interval}).get("data", {}) or {}

    def balance_sheet(self, isin: str, interval: str = "annual") -> dict:
        return self._get(f"/fundamentals/{isin}/balance-sheet", {"interval": interval}).get("data", {}) or {}

    def cash_flow(self, isin: str, interval: str = "annual") -> dict:
        return self._get(f"/fundamentals/{isin}/cash-flow", {"interval": interval}).get("data", {}) or {}

    def key_ratios(self, isin: str) -> list:
        return self._get(f"/fundamentals/{isin}/key-ratios").get("data", []) or []

    def share_holdings(self, isin: str) -> list:
        return self._get(f"/fundamentals/{isin}/share-holdings").get("data", []) or []

    def corporate_actions(self, isin: str) -> list:
        return self._get(f"/fundamentals/{isin}/corporate-actions").get("data", []) or []

    def competitors(self, instrument_key: str) -> list:
        return self._get(f"/fundamentals/{self._q(instrument_key)}/competitors").get("data", []) or []

    # -------------------- Options analytics -------------------- #
    def pcr(self, instrument_key: str, date: str, expiry: str = "current_month",
            bucket_interval: int = 60) -> dict | None:
        return self._get("/market/pcr", {"instrument_key": instrument_key, "expiry": expiry,
                                          "date": date, "bucket_interval": bucket_interval}).get("data")

    def max_pain(self, instrument_key: str, date: str, expiry: str = "current_month",
                 bucket_interval: int = 60) -> dict | None:
        return self._get("/market/max-pain", {"instrument_key": instrument_key, "expiry": expiry,
                                               "date": date, "bucket_interval": bucket_interval}).get("data")

    def oi(self, instrument_key: str, date: str, expiry: str = "current_month",
           bucket_interval: int = 60) -> dict | None:
        return self._get("/market/oi", {"instrument_key": instrument_key, "expiry": expiry,
                                         "date": date, "bucket_interval": bucket_interval}).get("data")

    # -------------------- FII / DII flows -------------------- #
    def fii(self, data_types: list[str], interval: str = "1D", from_date: str | None = None) -> dict:
        params = [("data_type", d) for d in data_types] + [("interval", interval)]
        if from_date:
            params.append(("from", from_date))
        return self._get("/market/fii", params).get("data", {}) or {}

    def dii(self, data_types: list[str], interval: str = "1D", from_date: str | None = None) -> dict:
        params = [("data_type", d) for d in data_types] + [("interval", interval)]
        if from_date:
            params.append(("from", from_date))
        return self._get("/market/dii", params).get("data", {}) or {}

    # -------------------- Market -------------------- #
    def holidays(self, date: str | None = None) -> list:
        path = f"/market/holidays/{date}" if date else "/market/holidays"
        return self._get(path).get("data", []) or []

    def exchange_status(self, exchange: str = "NSE") -> dict:
        return self._get(f"/market/status/{exchange}").get("data", {}) or {}
