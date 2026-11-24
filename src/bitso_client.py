"""
Cliente REST para la API de Bitso (https://api.bitso.com/v3).

Cubre los endpoints publicos (ticker, order_book, trades, available_books)
y los endpoints privados firmados (balance, ordenes, user_trades) necesarios
para el bot. Implementa el esquema de firma documentado por Bitso:

    Authorization: Bitso <key>:<nonce>:<signature>
    signature = HMAC_SHA256(secret, nonce + METHOD + request_path + json_body).hexdigest()

Nonce v2: entero de 13 digitos (epoch ms) + sufijo aleatorio de 6 digitos,
estrictamente creciente dentro del proceso (se fuerza con un contador local
por si dos llamadas caen en el mismo milisegundo).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests


class BitsoAPIError(RuntimeError):
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"Bitso API error (HTTP {status_code}): {payload}")


class _NonceGenerator:
    """Genera nonces v2 unicos y estrictamente crecientes dentro del proceso."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0

    def next(self) -> str:
        with self._lock:
            ms = int(time.time() * 1000)
            salt = random.randint(100000, 999999)
            nonce = int(f"{ms}{salt}")
            if nonce <= self._last:
                nonce = self._last + 1
            self._last = nonce
            return str(nonce)


@dataclass
class OrderResult:
    oid: str
    raw: dict


class BitsoClient:
    def __init__(self, base_url: str, api_key: str | None = None, api_secret: str | None = None,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self._session = requests.Session()
        self._nonces = _NonceGenerator()

    # ---------------------------------------------------------------- utils

    def _request_path(self, path: str) -> str:
        # El path firmado debe incluir exactamente lo que se envia (sin host),
        # p.ej. "/v3/orders" (el base_url ya termina en .../v3).
        base_path = requests.utils.urlparse(self.base_url).path
        return f"{base_path}{path}"

    def _sign(self, method: str, request_path: str, body: str) -> tuple[str, str]:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Faltan BITSO_API_KEY / BITSO_API_SECRET para un endpoint privado.")
        nonce = self._nonces.next()
        message = f"{nonce}{method.upper()}{request_path}{body}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return nonce, signature

    def _call(self, method: str, path: str, *, params: dict | None = None,
               body_obj: dict | None = None, private: bool) -> Any:
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json"}
        body_str = ""
        if body_obj is not None:
            body_str = json.dumps(body_obj, separators=(",", ":"))

        if private:
            request_path = self._request_path(path)
            if params:
                # Si hay query string, Bitso espera que forme parte del path firmado.
                qs = "&".join(f"{k}={v}" for k, v in params.items())
                request_path = f"{request_path}?{qs}"
            nonce, signature = self._sign(method, request_path, body_str)
            headers["Authorization"] = f"Bitso {self.api_key}:{nonce}:{signature}"

        resp = self._session.request(
            method, url, params=params,
            data=body_str if body_obj is not None else None,
            headers=headers, timeout=self.timeout,
        )
        try:
            data = resp.json()
        except ValueError:
            resp.raise_for_status()
            raise

        if not resp.ok or data.get("success") is False:
            raise BitsoAPIError(resp.status_code, data)
        return data.get("payload")

    # ------------------------------------------------------------- publicos

    def available_books(self) -> list[dict]:
        return self._call("GET", "/available_books", private=False)

    def ticker(self, book: str) -> dict:
        return self._call("GET", "/ticker", params={"book": book}, private=False)

    def order_book(self, book: str, aggregate: bool = True) -> dict:
        return self._call(
            "GET", "/order_book",
            params={"book": book, "aggregate": str(aggregate).lower()},
            private=False,
        )

    def trades(self, book: str, limit: int = 100, marker: str | None = None,
               sort: str = "desc") -> list[dict]:
        params = {"book": book, "limit": limit, "sort": sort}
        if marker:
            params["marker"] = marker
        return self._call("GET", "/trades", params=params, private=False)

    # -------------------------------------------------------------- privados

    def balance(self) -> dict:
        return self._call("GET", "/balance", private=True)

    def place_order(self, book: str, side: str, order_type: str, major: str,
                     price: str | None = None, time_in_force: str | None = None,
                     origin_id: str | None = None) -> OrderResult:
        if side not in ("buy", "sell"):
            raise ValueError("side debe ser 'buy' o 'sell'")
        if order_type not in ("market", "limit"):
            raise ValueError("order_type debe ser 'market' o 'limit'")
        if order_type == "limit" and price is None:
            raise ValueError("Las ordenes limit requieren 'price'")

        body: dict = {"book": book, "side": side, "type": order_type, "major": major}
        if price is not None:
            body["price"] = price
        if time_in_force is not None:
            body["time_in_force"] = time_in_force
        if origin_id is not None:
            body["origin_id"] = origin_id

        payload = self._call("POST", "/orders", body_obj=body, private=True)
        return OrderResult(oid=payload["oid"], raw=payload)

    def cancel_order(self, oid: str) -> list[str]:
        return self._call("DELETE", f"/orders/{oid}", private=True)

    def lookup_orders(self, oids: list[str]) -> list[dict]:
        return self._call("GET", "/orders", params={"oids": ",".join(oids)}, private=True)

    def open_orders(self, book: str | None = None) -> list[dict]:
        params = {"book": book} if book else None
        return self._call("GET", "/open_orders", params=params, private=True)

    def user_trades(self, book: str, limit: int = 25, marker: str | None = None,
                     sort: str = "desc") -> list[dict]:
        params = {"book": book, "limit": limit, "sort": sort}
        if marker:
            params["marker"] = marker
        return self._call("GET", "/user_trades", params=params, private=True)
