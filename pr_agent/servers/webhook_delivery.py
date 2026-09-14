"""Delivery-level idempotency for background GitHub webhook dispatch."""

import asyncio
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from pr_agent.log import get_logger

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS github_webhook_deliveries (
    installation_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    state TEXT NOT NULL,
    claim_token TEXT NOT NULL,
    lease_until REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (installation_id, delivery_id)
)
"""


class WebhookDeliveryStore:
    """Persist delivery claims so duplicate webhooks cannot cross worker boundaries."""

    def __init__(self, database_path: str, *, lease_ttl: int, retention_ttl: int):
        if lease_ttl <= 0:
            raise ValueError("webhook delivery lease TTL must be positive")
        if retention_ttl < lease_ttl:
            raise ValueError("webhook delivery retention TTL must cover the lease TTL")
        self.database_path = database_path if database_path == ":memory:" else str(Path(database_path).expanduser())
        self.lease_ttl = lease_ttl
        self.retention_ttl = retention_ttl

    def _connect(self):
        if self.database_path != ":memory:":
            Path(self.database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _claim_sync(self, installation_id: str, delivery_id: str) -> str | None:
        now = time.time()
        claim_token = uuid.uuid4().hex
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(_CREATE_TABLE)
            connection.execute(
                "DELETE FROM github_webhook_deliveries WHERE expires_at <= ?",
                (now,),
            )
            row = connection.execute(
                """
                SELECT state, claim_token, lease_until
                FROM github_webhook_deliveries
                WHERE installation_id = ? AND delivery_id = ?
                """,
                (installation_id, delivery_id),
            ).fetchone()
            if row:
                state, _, lease_until = row
                if state == "completed" or (state == "in_flight" and lease_until > now):
                    connection.rollback()
                    return None
                connection.execute(
                    """
                    UPDATE github_webhook_deliveries
                    SET state = 'in_flight', claim_token = ?, lease_until = ?, expires_at = ?
                    WHERE installation_id = ? AND delivery_id = ?
                    """,
                    (
                        claim_token,
                        now + self.lease_ttl,
                        now + self.retention_ttl,
                        installation_id,
                        delivery_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO github_webhook_deliveries
                        (installation_id, delivery_id, state, claim_token, lease_until, expires_at)
                    VALUES (?, ?, 'in_flight', ?, ?, ?)
                    """,
                    (
                        installation_id,
                        delivery_id,
                        claim_token,
                        now + self.lease_ttl,
                        now + self.retention_ttl,
                    ),
                )
            connection.commit()
            return claim_token
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _complete_sync(self, installation_id: str, delivery_id: str, claim_token: str) -> bool:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE github_webhook_deliveries
                SET state = 'completed', claim_token = '', lease_until = ?, expires_at = ?
                WHERE installation_id = ? AND delivery_id = ?
                  AND state = 'in_flight' AND claim_token = ?
                """,
                (0, time.time() + self.retention_ttl, installation_id, delivery_id, claim_token),
            )
            connection.commit()
            return cursor.rowcount == 1
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _release_sync(self, installation_id: str, delivery_id: str, claim_token: str) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM github_webhook_deliveries
                WHERE installation_id = ? AND delivery_id = ? AND claim_token = ?
                """,
                (installation_id, delivery_id, claim_token),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def claim(self, installation_id: str, delivery_id: str) -> str | None:
        return await asyncio.to_thread(self._claim_sync, installation_id, delivery_id)

    async def complete(self, installation_id: str, delivery_id: str, claim_token: str) -> bool:
        return await asyncio.to_thread(self._complete_sync, installation_id, delivery_id, claim_token)

    async def release(self, installation_id: str, delivery_id: str, claim_token: str) -> None:
        await asyncio.to_thread(self._release_sync, installation_id, delivery_id, claim_token)


@asynccontextmanager
async def webhook_delivery_slot(
    delivery_id: str | None,
    installation_id: str | None,
    *,
    database_path: str,
    lease_ttl: int,
    retention_ttl: int,
) -> AsyncIterator[bool]:
    """Claim one delivery, releasing failed work so a later redelivery can retry."""
    if not delivery_id:
        yield True
        return

    normalized_installation_id = str(installation_id or "")
    normalized_delivery_id = str(delivery_id)
    store = WebhookDeliveryStore(
        database_path,
        lease_ttl=lease_ttl,
        retention_ttl=retention_ttl,
    )
    claim_token = await store.claim(normalized_installation_id, normalized_delivery_id)
    if claim_token is None:
        get_logger().info(
            f"Skipping duplicate GitHub webhook delivery {normalized_delivery_id=} for {normalized_installation_id=}"
        )
        yield False
        return

    try:
        yield True
    except BaseException:
        await store.release(normalized_installation_id, normalized_delivery_id, claim_token)
        raise
    completed = await store.complete(normalized_installation_id, normalized_delivery_id, claim_token)
    if not completed:
        get_logger().warning(
            f"GitHub webhook delivery claim was lost before completion: {normalized_delivery_id=} "
            f"{normalized_installation_id=}"
        )
