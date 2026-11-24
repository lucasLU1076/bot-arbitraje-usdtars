"""
Registro opcional de cada orden en un Google Sheet, via un Web App de Apps
Script (evita manejar credenciales de Google Cloud: alcanza con una URL +
un token secreto, generados desde el propio Sheet).

Disenado para NUNCA interrumpir el loop de trading: si no esta configurado,
o si falla la red, o si Google tarda, se loggea un aviso y el bot sigue
operando exactamente igual. El registro en el Google Sheet es solo para que
el usuario pueda ver las ordenes ordenadas; la fuente de verdad sigue siendo
siempre el SQLite local (data_store.py).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

HEADER = [
    "fecha_apertura", "fecha_cierre", "modo", "lado", "precio_referencia",
    "precio_entrada", "precio_objetivo", "precio_cierre", "motivo_cierre",
    "monto_ars", "cantidad_usdt", "fee_estimado_ars", "pnl_ars", "pnl_pct",
    "oid", "notas",
]


class SheetsLogger:
    def __init__(self, webhook_url: str | None, token: str | None, log_fn=print, timeout: float = 6.0):
        self.webhook_url = webhook_url
        self.token = token
        self.enabled = bool(webhook_url and token)
        self._log = log_fn
        self.timeout = timeout

    def _post(self, payload: dict) -> dict | None:
        if not self.enabled:
            return None
        body = {**payload, "token": self.token}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url, data=data, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if not result.get("ok"):
                self._log(f"Google Sheet: el webhook devolvio error ({result.get('error')}). Sigo igual.")
                return None
            return result
        except Exception as exc:  # noqa: BLE001 - un fallo aca jamas debe tumbar el bot
            self._log(f"Google Sheet: no se pudo registrar (red/timeout), se sigue operando igual: {exc}")
            return None

    def append_row(self, row: dict) -> int | None:
        values = [row.get(col, "") for col in HEADER]
        result = self._post({"action": "append", "row": values})
        return result.get("row_number") if result else None

    def update_row(self, row_number: int | None, row: dict) -> None:
        if row_number is None:
            return
        values = [row.get(col, "") for col in HEADER]
        self._post({"action": "update", "row_number": row_number, "row": values})
