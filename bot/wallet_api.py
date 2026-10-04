"""Cienki klient BudgetBakers Wallet REST API (https://rest.budgetbakers.com/wallet/openapi/ui)."""

import asyncio
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Stałe ID kategorii systemowych (identyczne u każdego użytkownika).
UNKNOWN_EXPENSE = "5c5c32c9-0082-8000-8000-000000000000"
UNKNOWN_INCOME = "5c5c32c8-0082-8000-8000-000000000000"


class WalletError(Exception):
    pass


class WalletAPI:
    def __init__(self, token: str, base_url: str):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        for attempt in range(4):
            resp = await self._http.request(method, path, **kwargs)
            if resp.status_code == 409:  # trwa synchronizacja danych
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                await asyncio.sleep(int(resp.headers.get("Retry-After", "30")))
                continue
            if resp.status_code in (401, 403):
                raise WalletError(f"Wallet odrzucił token ({resp.status_code}): {resp.text[:200]}")
            # 207 i 400 przy zapisie mają ten sam format wsadowy: sprawdza je wywołujący.
            if resp.status_code >= 500 and method == "GET" and attempt < 3:
                await asyncio.sleep(3)
                continue
            if resp.status_code >= 400 and resp.status_code != 400:
                raise WalletError(f"Wallet API {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise WalletError("Wallet API: przekroczono liczbę ponowień (sync/limit)")

    async def _paged(self, path: str, key: str, params: dict | None = None) -> list[dict]:
        params = dict(params or {})
        params.setdefault("limit", 200)
        offset, items = 0, []
        while True:
            data = await self._request("GET", path, params={**params, "offset": offset})
            items.extend(data.get(key, []))
            if data.get("nextOffset") is None:
                return items
            offset = data["nextOffset"]

    async def accounts(self) -> list[dict]:
        return await self._paged("/v1/api/accounts", "accounts", {"archived": "false"})

    async def categories(self) -> list[dict]:
        return await self._paged("/v1/api/categories", "categories", {"archived": "false"})

    async def records(self, date_from: str, date_to: str, **filters) -> list[dict]:
        params = {"recordDate": [f"gte.{date_from}", f"lt.{date_to}"], **filters}
        return await self._paged("/v1/api/records", "records", params)

    async def create_records(self, records: list[dict]) -> list[dict]:
        """Zwraca listę wyników (inputIndex, success, id, error) — po jednym na rekord."""
        data = await self._request(
            "POST", "/v1/api/records", params={"returnData": "false"}, json=records
        )
        return data.get("results", [])

    async def delete_records(self, ids: list[str]) -> None:
        for i in range(0, len(ids), 10):
            await self._request("DELETE", "/v1/api/records", json={"ids": ids[i : i + 10]})
