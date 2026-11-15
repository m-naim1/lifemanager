"""Cloudflare D1 database driver.

D1 is Cloudflare's serverless SQLite. A container cannot reach D1 through a
socket, so this driver talks to the D1 REST API:

    POST /accounts/{account_id}/d1/database/{database_id}/query

It mimics the small slice of the aiosqlite API that the repositories use, so
no repository code changes are needed:

    conn.execute(sql, params)  ->  cursor
    cursor.fetchone() / fetchall() / cursor.lastrowid
    conn.commit()   (D1 auto-commits every statement; commit() is a no-op)
    conn.close()

Rows are plain dicts, which satisfies both `row["column"]` access and
`dict(row)` as used by the repositories.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import settings


class D1Error(RuntimeError):
    """Raised when a D1 query fails or the API returns an error."""


class D1Cursor:
    """Cursor-like object over one D1 result set."""

    def __init__(self, rows: list[dict[str, Any]], last_row_id: int = 0) -> None:
        self._rows = rows
        self._index = 0
        self.lastrowid = last_row_id

    async def fetchone(self) -> dict[str, Any] | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    async def fetchall(self) -> list[dict[str, Any]]:
        rows = self._rows[self._index :]
        self._index = len(self._rows)
        return rows


class D1Connection:
    """Connection-like object over the D1 REST API."""

    def __init__(
        self,
        account_id: str,
        database_id: str,
        api_token: str,
        base_url: str = "https://api.cloudflare.com/client/v4",
    ) -> None:
        self._path = f"/accounts/{account_id}/d1/database/{database_id}/query"
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(30.0),
        )

    async def execute(self, sql: str, params: Any = ()) -> D1Cursor:
        """Run a single statement. D1 auto-commits; there is no open transaction."""
        result = await self._post({"sql": sql, "params": list(params)})
        meta = result.get("meta") or {}
        rows = result.get("results") or []
        return D1Cursor(rows, int(meta.get("last_row_id") or 0))

    async def batch(self, statements: list[tuple[str, Any]]) -> list[D1Cursor]:
        """Run several statements in one request, sequentially."""
        response = await self._client.post(
            self._path,
            json={"batch": [{"sql": sql, "params": list(params)} for sql, params in statements]},
        )
        try:
            data = response.json()
        except ValueError:
            raise D1Error(
                f"D1 API returned non-JSON response (HTTP {response.status_code}): "
                f"{response.text[:500]}"
            ) from None
        if response.status_code != 200 or not data.get("success"):
            errors = data.get("errors") or []
            detail = "; ".join(
                str(err.get("message", err)) for err in errors if isinstance(err, dict)
            ) or f"HTTP {response.status_code}"
            raise D1Error(f"D1 batch failed: {detail}")
        results = data.get("result") or []
        return [
            D1Cursor(
                item.get("results") or [],
                int((item.get("meta") or {}).get("last_row_id") or 0),
            )
            for item in results
        ]

    async def commit(self) -> None:
        """D1 auto-commits every statement; kept for aiosqlite call compatibility."""
        return None

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(self._path, json=payload)
        try:
            data = response.json()
        except ValueError:
            raise D1Error(
                f"D1 API returned non-JSON response (HTTP {response.status_code}): "
                f"{response.text[:500]}"
            ) from None
        if response.status_code != 200 or not data.get("success"):
            errors = data.get("errors") or []
            detail = "; ".join(
                str(err.get("message", err)) for err in errors if isinstance(err, dict)
            ) or f"HTTP {response.status_code}"
            raise D1Error(f"D1 query failed: {detail}")
        results = data.get("result") or []
        if not results:
            raise D1Error("D1 query returned no result entries")
        first = results[0]
        if first.get("success") is False:
            raise D1Error(f"D1 statement failed: {first}")
        return first


class D1Database:
    """Drop-in replacement for `app.db.Database` when running against D1."""

    def __init__(
        self,
        account_id: str = "",
        database_id: str = "",
        api_token: str = "",
        base_url: str = "",
    ) -> None:
        account_id = account_id or settings.d1_account_id
        database_id = database_id or settings.d1_database_id
        api_token = api_token or settings.d1_api_token
        base_url = base_url or settings.d1_api_base_url
        missing = [
            name
            for name, value in (
                ("LIFEMANAGER_D1_ACCOUNT_ID", account_id),
                ("LIFEMANAGER_D1_DATABASE_ID", database_id),
                ("LIFEMANAGER_D1_API_TOKEN", api_token),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "D1 driver selected but missing configuration: " + ", ".join(missing)
            )
        self.conn = D1Connection(account_id, database_id, api_token, base_url)

    async def connect(self) -> None:
        # Verify connectivity and credentials early so misconfiguration shows
        # up at startup, not on the first request.
        cursor = await self.conn.execute("SELECT 1 AS ok")
        row = await cursor.fetchone()
        if row is None or row.get("ok") != 1:
            raise D1Error("D1 connectivity check failed")
        if settings.auto_migrate:
            from .migrations import apply_pending_migrations

            applied = await apply_pending_migrations(self.conn)
            if applied:
                print(f"[d1] applied migrations: {', '.join(applied)}")

    async def close(self) -> None:
        await self.conn.close()
