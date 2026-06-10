from __future__ import annotations

import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from .config import Settings
from .interface import default_app_settings


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def future_iso(*, days: int = 0, hours: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(days=days, hours=hours)).replace(microsecond=0).isoformat()


def generate_public_id() -> str:
    return uuid.uuid4().hex[:12].upper()


def generate_referral_code() -> str:
    return uuid.uuid4().hex[:8].upper()


@dataclass(slots=True)
class AppSetting:
    key: str
    value: str


@dataclass(slots=True)
class OrderPricing:
    base_amount_rub: int
    discount_percent: int
    discount_rub: int
    bonus_applied_rub: int
    final_amount_rub: int
    referred_discount_active: bool
    loyalty_discount_active: bool
    delivered_orders_count: int
    bonus_balance_before_rub: int


def _to_json(values: list[int]) -> str:
    return json.dumps(values, separators=(",", ":"))


def _from_json(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    result: list[int] = []
    for item in data:
        if not isinstance(item, (int, float, str)):
            continue
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _coerce_int(value: str | int | None, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_percent(value: str | int | None, default: int = 0) -> int:
    return max(0, min(100, _coerce_int(value, default)))


TRIAL_PLAN_NAME = "Пробный VPN на 3 дня"
TRIAL_PLAN_DESCRIPTION = "Бесплатный пробный доступ на 3 дня"
TRIAL_DURATION_DAYS = 3
TRIAL_PRICE_RUB = 0
TRIAL_ACTIVE_STATUSES = ("pending_payment", "waiting_review", "paid", "delivered")


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self, settings: Settings) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tg_users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    referral_code TEXT,
                    referred_by_user_id INTEGER,
                    referral_bonus_balance_rub INTEGER NOT NULL DEFAULT 0,
                    total_referral_earned_rub INTEGER NOT NULL DEFAULT 0,
                    referral_invites_count INTEGER NOT NULL DEFAULT 0,
                    referral_reward_granted INTEGER NOT NULL DEFAULT 0,
                    is_partner INTEGER NOT NULL DEFAULT 0,
                    partner_name TEXT NOT NULL DEFAULT '',
                    partner_commission_percent INTEGER NOT NULL DEFAULT 0,
                    partner_balance_rub INTEGER NOT NULL DEFAULT 0,
                    total_partner_earned_rub INTEGER NOT NULL DEFAULT 0,
                    partner_paid_out_rub INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    price_rub INTEGER NOT NULL,
                    duration_days INTEGER NOT NULL,
                    traffic_gb INTEGER NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    inbound_ids TEXT NOT NULL DEFAULT '[]',
                    is_trial INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_id TEXT UNIQUE NOT NULL,
                    tg_user_id INTEGER NOT NULL,
                    plan_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payment_provider TEXT NOT NULL DEFAULT '',
                    payment_transaction_id TEXT,
                    payment_status TEXT,
                    payment_url TEXT,
                    payment_method TEXT,
                    payment_currency TEXT NOT NULL DEFAULT 'RUB',
                    payment_raw_payload TEXT NOT NULL DEFAULT '',
                    amount_rub INTEGER NOT NULL,
                    base_amount_rub INTEGER NOT NULL DEFAULT 0,
                    discount_rub INTEGER NOT NULL DEFAULT 0,
                    bonus_applied_rub INTEGER NOT NULL DEFAULT 0,
                    applied_discount_percent INTEGER NOT NULL DEFAULT 0,
                    bonus_refunded INTEGER NOT NULL DEFAULT 0,
                    partner_reward_rub INTEGER NOT NULL DEFAULT 0,
                    partner_reward_granted INTEGER NOT NULL DEFAULT 0,
                    is_trial INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payment_note TEXT NOT NULL DEFAULT '',
                    xui_email TEXT,
                    xui_subscription_url TEXT,
                    xui_links_json TEXT,
                    provisioning_error TEXT,
                    FOREIGN KEY(tg_user_id) REFERENCES tg_users(id),
                    FOREIGN KEY(plan_id) REFERENCES plans(id)
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS partner_access_tokens (
                    user_id INTEGER PRIMARY KEY,
                    token TEXT UNIQUE NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES tg_users(id)
                );

                CREATE TABLE IF NOT EXISTS partner_payouts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount_rub INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES tg_users(id)
                );

                CREATE TABLE IF NOT EXISTS partner_login_codes (
                    user_id INTEGER PRIMARY KEY,
                    code TEXT UNIQUE NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    used_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES tg_users(id)
                );

                CREATE TABLE IF NOT EXISTS partner_withdraw_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount_rub INTEGER NOT NULL,
                    payout_details TEXT NOT NULL DEFAULT '',
                    balance_reserved INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    processed_at TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(user_id) REFERENCES tg_users(id)
                );

                CREATE TABLE IF NOT EXISTS partner_terms_accept_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    terms_version TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    ip_address TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(user_id) REFERENCES tg_users(id)
                );
                """
            )
            self._run_migrations(conn)
            for key, value in default_app_settings(settings).items():
                self.insert_setting_if_missing(conn, key, value)

    def _run_migrations(self, conn: sqlite3.Connection) -> None:
        self._ensure_column(conn, "tg_users", "referral_code", "TEXT")
        self._ensure_column(conn, "tg_users", "referred_by_user_id", "INTEGER")
        self._ensure_column(conn, "tg_users", "referral_bonus_balance_rub", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "tg_users", "total_referral_earned_rub", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "tg_users", "referral_invites_count", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "tg_users", "referral_reward_granted", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "tg_users", "is_partner", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "tg_users", "partner_name", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "tg_users", "partner_commission_percent", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "tg_users", "partner_balance_rub", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "tg_users", "total_partner_earned_rub", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "tg_users", "partner_paid_out_rub", "INTEGER NOT NULL DEFAULT 0")

        self._ensure_column(conn, "plans", "is_trial", "INTEGER NOT NULL DEFAULT 0")

        self._ensure_column(conn, "orders", "base_amount_rub", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "orders", "discount_rub", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "orders", "bonus_applied_rub", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "orders", "applied_discount_percent", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "orders", "bonus_refunded", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "orders", "partner_reward_rub", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "orders", "partner_reward_granted", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "orders", "is_trial", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column(conn, "orders", "payment_provider", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "orders", "payment_transaction_id", "TEXT")
        self._ensure_column(conn, "orders", "payment_status", "TEXT")
        self._ensure_column(conn, "orders", "payment_url", "TEXT")
        self._ensure_column(conn, "orders", "payment_method", "TEXT")
        self._ensure_column(conn, "orders", "payment_currency", "TEXT NOT NULL DEFAULT 'RUB'")
        self._ensure_column(conn, "orders", "payment_raw_payload", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "partner_withdraw_requests", "payout_details", "TEXT NOT NULL DEFAULT ''")
        self._ensure_column(conn, "partner_withdraw_requests", "balance_reserved", "INTEGER NOT NULL DEFAULT 0")

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_tg_users_referral_code
            ON tg_users(referral_code)
            WHERE referral_code IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_transaction_id
            ON orders(payment_transaction_id)
            WHERE payment_transaction_id IS NOT NULL AND TRIM(payment_transaction_id) != ''
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_partner_access_tokens_token
            ON partner_access_tokens(token)
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_partner_login_codes_code
            ON partner_login_codes(code)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_partner_terms_accept_logs_user_version
            ON partner_terms_accept_logs(user_id, terms_version, accepted_at DESC)
            """
        )
        self._backfill_referral_codes(conn)
        conn.execute(
            """
            UPDATE orders
            SET base_amount_rub = amount_rub
            WHERE base_amount_rub = 0 AND amount_rub > 0
            """
        )

    def _table_columns(self, conn: sqlite3.Connection, table_name: str) -> set[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return {str(row["name"]) for row in rows}

    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
        if column_name in self._table_columns(conn, table_name):
            return
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}")

    def _create_referral_code(self, conn: sqlite3.Connection) -> str:
        while True:
            code = generate_referral_code()
            exists = conn.execute(
                "SELECT 1 FROM tg_users WHERE referral_code = ?",
                (code,),
            ).fetchone()
            if exists is None:
                return code

    def _backfill_referral_codes(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT id
            FROM tg_users
            WHERE referral_code IS NULL OR TRIM(referral_code) = ''
            """
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE tg_users SET referral_code = ? WHERE id = ?",
                (self._create_referral_code(conn), int(row["id"])),
            )

    def insert_setting_if_missing(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO app_settings(key, value) VALUES(?, ?)
            """,
            (key, value),
        )

    def upsert_setting(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """
            INSERT INTO app_settings(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            self.upsert_setting(conn, key, value)

    def get_settings(self) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, value FROM app_settings").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def get_int_setting(self, key: str, default: int = 0) -> int:
        return _coerce_int(self.get_settings().get(key), default)

    def get_loyalty_settings(self) -> dict[str, int]:
        values = self.get_settings()
        return {
            "referral_reward_percent": _coerce_int(values.get("referral_reward_percent"), 15),
            "referral_new_user_discount_percent": _coerce_int(
                values.get("referral_new_user_discount_percent"),
                10,
            ),
            "loyalty_discount_percent": _coerce_int(values.get("loyalty_discount_percent"), 7),
            "loyalty_orders_threshold": _coerce_int(values.get("loyalty_orders_threshold"), 3),
            "max_bonus_writeoff_percent": _coerce_int(values.get("max_bonus_writeoff_percent"), 30),
        }

    def upsert_user(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> sqlite3.Row:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tg_users(
                    telegram_id, username, first_name, last_name, created_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    last_seen_at = excluded.last_seen_at
                """,
                (telegram_id, username, first_name, last_name, now, now),
            )
            row = conn.execute(
                "SELECT * FROM tg_users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            assert row is not None
            if not row["referral_code"]:
                conn.execute(
                    "UPDATE tg_users SET referral_code = ? WHERE id = ?",
                    (self._create_referral_code(conn), int(row["id"])),
                )
                row = conn.execute(
                    "SELECT * FROM tg_users WHERE telegram_id = ?",
                    (telegram_id,),
                ).fetchone()
        assert row is not None
        return row

    def get_user_by_telegram_id(self, telegram_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM tg_users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM tg_users WHERE id = ?",
                (user_id,),
            ).fetchone()

    def _partner_token_is_active(self, expires_at: str | None) -> bool:
        if not expires_at:
            return False
        try:
            return datetime.fromisoformat(expires_at) > datetime.now(UTC)
        except ValueError:
            return False

    def get_or_create_partner_access_token(self, user_id: int, *, ttl_days: int = 30) -> str:
        now = utc_now_iso()
        expires_at = future_iso(days=ttl_days)
        with self.connect() as conn:
            user = conn.execute(
                "SELECT id, is_partner FROM tg_users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user is None or int(user["is_partner"] or 0) != 1:
                raise ValueError("Partner access is unavailable")

            token_row = conn.execute(
                "SELECT token, expires_at FROM partner_access_tokens WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if token_row is not None and self._partner_token_is_active(str(token_row["expires_at"] or "")):
                conn.execute(
                    "UPDATE partner_access_tokens SET updated_at = ? WHERE user_id = ?",
                    (now, user_id),
                )
                return str(token_row["token"])

            token = secrets.token_urlsafe(24)
            conn.execute(
                """
                INSERT INTO partner_access_tokens(user_id, token, expires_at, created_at, updated_at, last_used_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                ON CONFLICT(user_id) DO UPDATE SET
                    token = excluded.token,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at,
                    last_used_at = NULL
                """,
                (user_id, token, expires_at, now, now),
            )
            return token

    def get_partner_by_access_token(self, token: str) -> sqlite3.Row | None:
        normalized = token.strip()
        if not normalized:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT u.*, t.expires_at
                FROM partner_access_tokens t
                JOIN tg_users u ON u.id = t.user_id
                WHERE t.token = ?
                """,
                (normalized,),
            ).fetchone()
            if row is None or int(row["is_partner"] or 0) != 1:
                return None
            if not self._partner_token_is_active(str(row["expires_at"] or "")):
                return None
            conn.execute(
                "UPDATE partner_access_tokens SET last_used_at = ?, updated_at = ? WHERE user_id = ?",
                (now := utc_now_iso(), now, int(row["id"])),
            )
            return conn.execute(
                "SELECT * FROM tg_users WHERE id = ?",
                (int(row["id"]),),
            ).fetchone()

    def _partner_code_is_active(self, expires_at: str | None, used_at: str | None) -> bool:
        if used_at:
            return False
        if not expires_at:
            return False
        try:
            return datetime.fromisoformat(expires_at) > datetime.now(UTC)
        except ValueError:
            return False

    def generate_partner_login_code(self, user_id: int, *, ttl_minutes: int = 10) -> str:
        now = utc_now_iso()
        expires_at = future_iso(hours=0)  # placeholder updated below
        expires_at = (datetime.now(UTC) + timedelta(minutes=max(1, ttl_minutes))).replace(microsecond=0).isoformat()
        with self.connect() as conn:
            user = conn.execute(
                "SELECT id, is_partner FROM tg_users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user is None or int(user["is_partner"] or 0) != 1:
                raise ValueError("Partner access is unavailable")

            while True:
                code = f"{secrets.randbelow(1_000_000):06d}"
                exists = conn.execute(
                    """
                    SELECT 1
                    FROM partner_login_codes
                    WHERE code = ? AND used_at IS NULL AND expires_at > ?
                    """,
                    (code, now),
                ).fetchone()
                if exists is None:
                    break

            conn.execute(
                """
                INSERT INTO partner_login_codes(user_id, code, expires_at, created_at, used_at)
                VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT(user_id) DO UPDATE SET
                    code = excluded.code,
                    expires_at = excluded.expires_at,
                    created_at = excluded.created_at,
                    used_at = NULL
                """,
                (user_id, code, expires_at, now),
            )
            return code

    def consume_partner_login_code(self, code: str, *, expected_user_id: int | None = None) -> sqlite3.Row | None:
        normalized = "".join(ch for ch in str(code).strip() if ch.isdigit())
        if not normalized:
            return None
        now = utc_now_iso()
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT plc.user_id, plc.code, plc.expires_at, plc.used_at, u.*
                FROM partner_login_codes plc
                JOIN tg_users u ON u.id = plc.user_id
                WHERE plc.code = ?
                """,
                (normalized,),
            ).fetchone()
            if row is None or int(row["is_partner"] or 0) != 1:
                return None
            if expected_user_id is not None and int(row["user_id"]) != int(expected_user_id):
                return None
            if not self._partner_code_is_active(str(row["expires_at"] or ""), str(row["used_at"] or "")):
                return None
            conn.execute(
                "UPDATE partner_login_codes SET used_at = ? WHERE user_id = ?",
                (now, int(row["user_id"])),
            )
            return conn.execute(
                "SELECT * FROM tg_users WHERE id = ?",
                (int(row["user_id"]),),
            ).fetchone()

    def get_partner_terms_acceptance(self, user_id: int, terms_version: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT id, user_id, terms_version, accepted_at, ip_address, user_agent
                FROM partner_terms_accept_logs
                WHERE user_id = ? AND terms_version = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id, terms_version.strip()),
            ).fetchone()

    def accept_partner_terms(
        self,
        user_id: int,
        *,
        terms_version: str,
        ip_address: str = "",
        user_agent: str = "",
    ) -> sqlite3.Row:
        version = terms_version.strip()
        if not version:
            raise ValueError("Terms version is required")
        with self.connect() as conn:
            existing = conn.execute(
                """
                SELECT id, user_id, terms_version, accepted_at, ip_address, user_agent
                FROM partner_terms_accept_logs
                WHERE user_id = ? AND terms_version = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id, version),
            ).fetchone()
            if existing is not None:
                return existing
            accepted_at = utc_now_iso()
            conn.execute(
                """
                INSERT INTO partner_terms_accept_logs(user_id, terms_version, accepted_at, ip_address, user_agent)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, version, accepted_at, ip_address.strip(), user_agent.strip()),
            )
            row = conn.execute(
                """
                SELECT id, user_id, terms_version, accepted_at, ip_address, user_agent
                FROM partner_terms_accept_logs
                WHERE id = last_insert_rowid()
                """
            ).fetchone()
            assert row is not None
            return row

    def get_user_by_referral_code(self, referral_code: str) -> sqlite3.Row | None:
        normalized = referral_code.strip().upper()
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM tg_users WHERE referral_code = ?",
                (normalized,),
            ).fetchone()

    def attach_referral(self, telegram_id: int, referral_code: str) -> bool:
        normalized = referral_code.strip().upper()
        if not normalized:
            return False
        with self.connect() as conn:
            user = conn.execute(
                "SELECT * FROM tg_users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()
            inviter = conn.execute(
                "SELECT * FROM tg_users WHERE referral_code = ?",
                (normalized,),
            ).fetchone()
            if user is None or inviter is None:
                return False
            if int(user["id"]) == int(inviter["id"]):
                return False
            if user["referred_by_user_id"]:
                return False
            conn.execute(
                "UPDATE tg_users SET referred_by_user_id = ? WHERE id = ?",
                (int(inviter["id"]), int(user["id"])),
            )
            return True

    def save_partner_config(
        self,
        user_id: int,
        *,
        is_partner: bool,
        partner_name: str,
        partner_commission_percent: int,
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tg_users
                SET is_partner = ?,
                    partner_name = ?,
                    partner_commission_percent = ?
                WHERE id = ?
                """,
                (
                    int(is_partner),
                    partner_name.strip(),
                    _coerce_percent(partner_commission_percent, 0) if is_partner else 0,
                    user_id,
                ),
            )
            return conn.execute(
                "SELECT * FROM tg_users WHERE id = ?",
                (user_id,),
            ).fetchone()

    def record_partner_payout(self, user_id: int, amount_rub: int) -> int:
        amount = max(0, int(amount_rub))
        if amount <= 0:
            return 0
        with self.connect() as conn:
            user = conn.execute(
                "SELECT partner_balance_rub FROM tg_users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                return 0
            applied = min(amount, max(0, int(user["partner_balance_rub"] or 0)))
            if applied <= 0:
                return 0
            conn.execute(
                """
                UPDATE tg_users
                SET partner_balance_rub = partner_balance_rub - ?,
                    partner_paid_out_rub = partner_paid_out_rub + ?
                WHERE id = ?
                """,
                (applied, applied, user_id),
            )
            conn.execute(
                """
                INSERT INTO partner_payouts(user_id, amount_rub, note, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, applied, "Manual payout", utc_now_iso()),
            )
            return applied

    def record_partner_payout_for_request(self, request_id: int, note: str = "") -> sqlite3.Row | None:
        with self.connect() as conn:
            request_row = conn.execute(
                """
                SELECT r.id, r.user_id, r.amount_rub, r.status, r.payout_details, r.balance_reserved,
                       u.telegram_id, u.username, u.first_name, u.partner_name
                FROM partner_withdraw_requests r
                JOIN tg_users u ON u.id = r.user_id
                WHERE r.id = ?
                """,
                (request_id,),
            ).fetchone()
            if request_row is None or str(request_row["status"]) != "pending":
                return None

            applied = max(0, int(request_row["amount_rub"] or 0))
            if int(request_row["balance_reserved"] or 0) == 1:
                conn.execute(
                    """
                    UPDATE tg_users
                    SET partner_paid_out_rub = partner_paid_out_rub + ?
                    WHERE id = ?
                    """,
                    (applied, int(request_row["user_id"])),
                )
            else:
                available_balance = conn.execute(
                    "SELECT partner_balance_rub FROM tg_users WHERE id = ?",
                    (int(request_row["user_id"]),),
                ).fetchone()
                if available_balance is None:
                    return None
                applied = min(
                    applied,
                    max(0, int(available_balance["partner_balance_rub"] or 0)),
                )
                if applied > 0:
                    conn.execute(
                        """
                        UPDATE tg_users
                        SET partner_balance_rub = partner_balance_rub - ?,
                            partner_paid_out_rub = partner_paid_out_rub + ?
                        WHERE id = ?
                        """,
                        (applied, applied, int(request_row["user_id"])),
                    )
            if applied <= 0:
                return None

            processed_at = utc_now_iso()
            payout_note = note.strip() or f"Withdraw request #{request_id}"
            conn.execute(
                """
                INSERT INTO partner_payouts(user_id, amount_rub, note, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (int(request_row["user_id"]), applied, payout_note, processed_at),
            )
            conn.execute(
                """
                UPDATE partner_withdraw_requests
                SET status = 'paid',
                    amount_rub = ?,
                    balance_reserved = 0,
                    processed_at = ?,
                    note = ?
                WHERE id = ?
                """,
                (applied, processed_at, payout_note, request_id),
            )
            return conn.execute(
                """
                SELECT r.id, r.user_id, r.amount_rub, r.payout_details, r.balance_reserved, r.status, r.created_at, r.processed_at, r.note,
                       u.telegram_id, u.username, u.first_name, u.partner_name
                FROM partner_withdraw_requests r
                JOIN tg_users u ON u.id = r.user_id
                WHERE r.id = ?
                """,
                (request_id,),
            ).fetchone()

    def update_partner_withdraw_request_status(self, request_id: int, status: str, note: str = "") -> sqlite3.Row | None:
        normalized = status.strip().lower()
        if normalized not in {"pending", "rejected"}:
            raise ValueError("Unsupported withdraw request status")
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT r.id, r.user_id, r.amount_rub, r.payout_details, r.balance_reserved, r.status, r.created_at, r.processed_at, r.note,
                       u.telegram_id, u.username, u.first_name, u.partner_name
                FROM partner_withdraw_requests r
                JOIN tg_users u ON u.id = r.user_id
                WHERE r.id = ?
                """,
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            current_status = str(row["status"] or "").strip().lower()
            if current_status == "paid":
                return None
            if normalized == "pending" and current_status != "rejected":
                return None
            amount_rub = max(0, int(row["amount_rub"] or 0))
            reserved = int(row["balance_reserved"] or 0) == 1
            if normalized == "rejected" and current_status == "pending" and reserved and amount_rub > 0:
                conn.execute(
                    """
                    UPDATE tg_users
                    SET partner_balance_rub = partner_balance_rub + ?
                    WHERE id = ?
                    """,
                    (amount_rub, int(row["user_id"])),
                )
                reserved = False
            if normalized == "pending" and current_status == "rejected" and not reserved and amount_rub > 0:
                user = conn.execute(
                    "SELECT partner_balance_rub FROM tg_users WHERE id = ?",
                    (int(row["user_id"]),),
                ).fetchone()
                if user is None or int(user["partner_balance_rub"] or 0) < amount_rub:
                    return None
                conn.execute(
                    """
                    UPDATE tg_users
                    SET partner_balance_rub = partner_balance_rub - ?
                    WHERE id = ?
                    """,
                    (amount_rub, int(row["user_id"])),
                )
                reserved = True
            processed_at = utc_now_iso() if normalized != "pending" else None
            conn.execute(
                """
                UPDATE partner_withdraw_requests
                SET status = ?, balance_reserved = ?, processed_at = ?, note = ?
                WHERE id = ?
                """,
                (normalized, 1 if reserved else 0, processed_at, note.strip(), request_id),
            )
            return conn.execute(
                """
                SELECT r.id, r.user_id, r.amount_rub, r.payout_details, r.balance_reserved, r.status, r.created_at, r.processed_at, r.note,
                       u.telegram_id, u.username, u.first_name, u.partner_name
                FROM partner_withdraw_requests r
                JOIN tg_users u ON u.id = r.user_id
                WHERE r.id = ?
                """,
                (request_id,),
            ).fetchone()

    def list_plans(self, active_only: bool = False, *, include_trial: bool = False) -> list[sqlite3.Row]:
        query = "SELECT * FROM plans"
        conditions: list[str] = []
        if active_only:
            conditions.append("is_active = 1")
        if not include_trial:
            conditions.append("COALESCE(is_trial, 0) = 0")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY price_rub ASC, id ASC"
        with self.connect() as conn:
            return conn.execute(query).fetchall()

    def get_plan(self, plan_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()

    def _collect_trial_inbound_ids(self, conn: sqlite3.Connection) -> list[int]:
        inbound_ids: set[int] = set()
        rows = conn.execute(
            """
            SELECT inbound_ids
            FROM plans
            WHERE is_active = 1 AND COALESCE(is_trial, 0) = 0
            ORDER BY id ASC
            """
        ).fetchall()
        for row in rows:
            inbound_ids.update(_from_json(row["inbound_ids"]))
        return sorted(inbound_ids)

    def _ensure_trial_plan_conn(self, conn: sqlite3.Connection) -> sqlite3.Row:
        now = utc_now_iso()
        inbound_ids = self._collect_trial_inbound_ids(conn)
        if not inbound_ids:
            raise ValueError("Trial is unavailable until at least one paid plan is configured")

        plan = conn.execute(
            """
            SELECT *
            FROM plans
            WHERE COALESCE(is_trial, 0) = 1
            ORDER BY id ASC
            LIMIT 1
            """
        ).fetchone()
        if plan is None:
            conn.execute(
                """
                INSERT INTO plans(
                    name, price_rub, duration_days, traffic_gb, description,
                    inbound_ids, is_trial, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?, ?)
                """,
                (
                    TRIAL_PLAN_NAME,
                    TRIAL_PRICE_RUB,
                    TRIAL_DURATION_DAYS,
                    0,
                    TRIAL_PLAN_DESCRIPTION,
                    _to_json(inbound_ids),
                    now,
                    now,
                ),
            )
            plan_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        else:
            plan_id = int(plan["id"])
            conn.execute(
                """
                UPDATE plans
                SET name = ?, price_rub = ?, duration_days = ?, traffic_gb = ?,
                    description = ?, inbound_ids = ?, is_trial = 1, is_active = 0, updated_at = ?
                WHERE id = ?
                """,
                (
                    TRIAL_PLAN_NAME,
                    TRIAL_PRICE_RUB,
                    TRIAL_DURATION_DAYS,
                    0,
                    TRIAL_PLAN_DESCRIPTION,
                    _to_json(inbound_ids),
                    now,
                    plan_id,
                ),
            )
        row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        assert row is not None
        return row

    def ensure_trial_plan(self) -> sqlite3.Row:
        with self.connect() as conn:
            return self._ensure_trial_plan_conn(conn)

    def save_plan(
        self,
        *,
        plan_id: int | None,
        name: str,
        price_rub: int,
        duration_days: int,
        traffic_gb: int,
        description: str,
        inbound_ids: list[int],
        is_active: bool,
    ) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            if plan_id is None:
                conn.execute(
                    """
                    INSERT INTO plans(
                        name, price_rub, duration_days, traffic_gb, description,
                        inbound_ids, is_trial, is_active, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        name,
                        price_rub,
                        duration_days,
                        traffic_gb,
                        description,
                        _to_json(inbound_ids),
                        int(is_active),
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE plans
                    SET name = ?, price_rub = ?, duration_days = ?, traffic_gb = ?,
                        description = ?, inbound_ids = ?, is_trial = 0, is_active = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        price_rub,
                        duration_days,
                        traffic_gb,
                        description,
                        _to_json(inbound_ids),
                        int(is_active),
                        now,
                        plan_id,
                    ),
                )

    def delete_plan(self, plan_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))

    def count_user_delivered_orders(self, tg_user_id: int) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM orders
                WHERE tg_user_id = ? AND status = 'delivered' AND COALESCE(is_trial, 0) = 0
                """,
                (tg_user_id,),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def _count_user_delivered_orders_conn(self, conn: sqlite3.Connection, tg_user_id: int) -> int:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM orders
            WHERE tg_user_id = ? AND status = 'delivered' AND COALESCE(is_trial, 0) = 0
            """,
            (tg_user_id,),
        ).fetchone()
        return int(row["total"]) if row is not None else 0

    def calculate_order_pricing(self, tg_user_id: int, plan_price_rub: int) -> OrderPricing:
        with self.connect() as conn:
            return self._calculate_order_pricing_conn(conn, tg_user_id, plan_price_rub)

    def _calculate_order_pricing_conn(
        self,
        conn: sqlite3.Connection,
        tg_user_id: int,
        plan_price_rub: int,
    ) -> OrderPricing:
        user = conn.execute("SELECT * FROM tg_users WHERE id = ?", (tg_user_id,)).fetchone()
        if user is None:
            raise ValueError("User not found")

        settings = self.get_loyalty_settings()
        delivered_orders_count = self._count_user_delivered_orders_conn(conn, tg_user_id)
        base_amount_rub = max(0, int(plan_price_rub))

        referred_discount_active = bool(user["referred_by_user_id"]) and delivered_orders_count == 0
        loyalty_discount_active = (
            settings["loyalty_orders_threshold"] > 0
            and delivered_orders_count >= settings["loyalty_orders_threshold"]
        )

        discount_percent = 0
        if referred_discount_active:
            discount_percent = max(discount_percent, settings["referral_new_user_discount_percent"])
        if loyalty_discount_active:
            discount_percent = max(discount_percent, settings["loyalty_discount_percent"])

        discount_rub = max(0, (base_amount_rub * max(0, discount_percent)) // 100)
        amount_after_discount = max(0, base_amount_rub - discount_rub)

        bonus_balance_before_rub = max(0, int(user["referral_bonus_balance_rub"] or 0))
        bonus_cap = max(0, (amount_after_discount * max(0, settings["max_bonus_writeoff_percent"])) // 100)
        bonus_applied_rub = min(bonus_balance_before_rub, bonus_cap, amount_after_discount)
        final_amount_rub = max(0, amount_after_discount - bonus_applied_rub)

        return OrderPricing(
            base_amount_rub=base_amount_rub,
            discount_percent=discount_percent,
            discount_rub=discount_rub,
            bonus_applied_rub=bonus_applied_rub,
            final_amount_rub=final_amount_rub,
            referred_discount_active=referred_discount_active and discount_percent > 0,
            loyalty_discount_active=loyalty_discount_active and discount_percent > 0,
            delivered_orders_count=delivered_orders_count,
            bonus_balance_before_rub=bonus_balance_before_rub,
        )

    def create_order(self, tg_user_id: int, plan_id: int) -> sqlite3.Row:
        now = utc_now_iso()
        public_id = generate_public_id()
        with self.connect() as conn:
            plan = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
            if plan is None:
                raise ValueError("Plan not found")

            pricing = self._calculate_order_pricing_conn(conn, tg_user_id, int(plan["price_rub"]))
            if pricing.bonus_applied_rub > 0:
                conn.execute(
                    """
                    UPDATE tg_users
                    SET referral_bonus_balance_rub = referral_bonus_balance_rub - ?
                    WHERE id = ?
                    """,
                    (pricing.bonus_applied_rub, tg_user_id),
                )

            conn.execute(
                """
                INSERT INTO orders(
                    public_id, tg_user_id, plan_id, status, amount_rub, base_amount_rub,
                    discount_rub, bonus_applied_rub, applied_discount_percent, is_trial,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, 'pending_payment', ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    public_id,
                    tg_user_id,
                    plan_id,
                    pricing.final_amount_rub,
                    pricing.base_amount_rub,
                    pricing.discount_rub,
                    pricing.bonus_applied_rub,
                    pricing.discount_percent,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT o.*, p.name AS plan_name, p.duration_days, p.traffic_gb, p.inbound_ids
                FROM orders o
                JOIN plans p ON p.id = o.plan_id
                WHERE o.public_id = ?
                """,
                (public_id,),
            ).fetchone()
        assert row is not None
        return row

    def _has_used_trial_conn(self, conn: sqlite3.Connection, tg_user_id: int) -> bool:
        placeholders = ", ".join("?" for _ in TRIAL_ACTIVE_STATUSES)
        params: list[object] = [tg_user_id, *TRIAL_ACTIVE_STATUSES]
        row = conn.execute(
            f"""
            SELECT 1
            FROM orders
            WHERE tg_user_id = ?
              AND COALESCE(is_trial, 0) = 1
              AND status IN ({placeholders})
            LIMIT 1
            """,
            params,
        ).fetchone()
        return row is not None

    def has_used_trial(self, tg_user_id: int) -> bool:
        with self.connect() as conn:
            return self._has_used_trial_conn(conn, tg_user_id)

    def create_trial_order(self, tg_user_id: int) -> sqlite3.Row:
        now = utc_now_iso()
        public_id = generate_public_id()
        with self.connect() as conn:
            if self._has_used_trial_conn(conn, tg_user_id):
                raise ValueError("Trial already used")

            trial_plan = self._ensure_trial_plan_conn(conn)
            conn.execute(
                """
                INSERT INTO orders(
                    public_id, tg_user_id, plan_id, status, amount_rub, base_amount_rub,
                    discount_rub, bonus_applied_rub, applied_discount_percent, is_trial,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, 'pending_payment', 0, 0, 0, 0, 0, 1, ?, ?)
                """,
                (
                    public_id,
                    tg_user_id,
                    int(trial_plan["id"]),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """
                SELECT o.*, p.name AS plan_name, p.duration_days, p.traffic_gb, p.inbound_ids
                FROM orders o
                JOIN plans p ON p.id = o.plan_id
                WHERE o.public_id = ?
                """,
                (public_id,),
            ).fetchone()
        assert row is not None
        return row

    def _refund_reserved_bonus_if_needed(self, conn: sqlite3.Connection, order_id: int) -> None:
        order = conn.execute(
            "SELECT id, tg_user_id, bonus_applied_rub, bonus_refunded, status FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        if order is None:
            return
        if int(order["bonus_refunded"] or 0) == 1:
            return
        if int(order["bonus_applied_rub"] or 0) <= 0:
            return
        if str(order["status"]) == "delivered":
            return
        conn.execute(
            """
            UPDATE tg_users
            SET referral_bonus_balance_rub = referral_bonus_balance_rub + ?
            WHERE id = ?
            """,
            (int(order["bonus_applied_rub"]), int(order["tg_user_id"])),
        )
        conn.execute(
            "UPDATE orders SET bonus_refunded = 1 WHERE id = ?",
            (order_id,),
        )

    def update_order_status(self, order_id: int, status: str, payment_note: str | None = None) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            if payment_note is None:
                conn.execute(
                    "UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, order_id),
                )
            else:
                conn.execute(
                    "UPDATE orders SET status = ?, payment_note = ?, updated_at = ? WHERE id = ?",
                    (status, payment_note, now, order_id),
                )
            if status in {"cancelled", "delivery_failed"}:
                self._refund_reserved_bonus_if_needed(conn, order_id)

    def update_order_payment(
        self,
        order_id: int,
        *,
        provider: str | None = None,
        transaction_id: str | None = None,
        provider_status: str | None = None,
        payment_url: str | None = None,
        payment_method: str | None = None,
        currency: str | None = None,
        raw_payload: dict[str, object] | str | None = None,
    ) -> None:
        now = utc_now_iso()
        assignments: list[str] = ["updated_at = ?"]
        params: list[object] = [now]
        if provider is not None:
            assignments.append("payment_provider = ?")
            params.append(provider)
        if transaction_id is not None:
            assignments.append("payment_transaction_id = ?")
            params.append(transaction_id)
        if provider_status is not None:
            assignments.append("payment_status = ?")
            params.append(provider_status)
        if payment_url is not None:
            assignments.append("payment_url = ?")
            params.append(payment_url)
        if payment_method is not None:
            assignments.append("payment_method = ?")
            params.append(payment_method)
        if currency is not None:
            assignments.append("payment_currency = ?")
            params.append(currency)
        if raw_payload is not None:
            assignments.append("payment_raw_payload = ?")
            if isinstance(raw_payload, str):
                params.append(raw_payload)
            else:
                params.append(json.dumps(raw_payload, ensure_ascii=True, separators=(",", ":")))
        params.append(order_id)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE orders SET {', '.join(assignments)} WHERE id = ?",
                params,
            )

    def get_order_by_payment_transaction_id(self, transaction_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT o.*,
                       COALESCE(p.name, 'Архивный тариф') AS plan_name,
                       COALESCE(p.duration_days, 0) AS duration_days,
                       COALESCE(p.traffic_gb, 0) AS traffic_gb,
                       COALESCE(p.description, '') AS description,
                       COALESCE(p.inbound_ids, '[]') AS inbound_ids,
                       u.telegram_id, u.username, u.first_name, u.last_name, u.referral_code
                FROM orders o
                LEFT JOIN plans p ON p.id = o.plan_id
                JOIN tg_users u ON u.id = o.tg_user_id
                WHERE o.payment_transaction_id = ?
                """,
                (transaction_id,),
            ).fetchone()

    def complete_order(
        self,
        order_id: int,
        *,
        status: str,
        xui_email: str,
        subscription_url: str | None,
        links: list[str],
        error: str | None,
    ) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE orders
                SET status = ?, xui_email = ?, xui_subscription_url = ?, xui_links_json = ?,
                    provisioning_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    xui_email,
                    subscription_url,
                    json.dumps(links, ensure_ascii=True),
                    error,
                    now,
                    order_id,
                ),
            )
            if status in {"cancelled", "delivery_failed"}:
                self._refund_reserved_bonus_if_needed(conn, order_id)

    def apply_referral_reward(self, order_id: int) -> dict[str, int | str] | None:
        with self.connect() as conn:
            order = conn.execute(
                """
                SELECT o.*, u.telegram_id, u.first_name, u.username, u.referred_by_user_id,
                       u.referral_reward_granted
                FROM orders o
                JOIN tg_users u ON u.id = o.tg_user_id
                WHERE o.id = ?
                """,
                (order_id,),
            ).fetchone()
            if order is None or str(order["status"]) != "delivered":
                return None
            if int(order["is_trial"] or 0) == 1 or int(order["amount_rub"] or 0) <= 0:
                return None
            if not order["referred_by_user_id"] or int(order["referral_reward_granted"] or 0) == 1:
                return None

            delivered_orders = self._count_user_delivered_orders_conn(conn, int(order["tg_user_id"]))
            inviter = conn.execute(
                "SELECT telegram_id, referral_bonus_balance_rub, is_partner FROM tg_users WHERE id = ?",
                (int(order["referred_by_user_id"]),),
            ).fetchone()
            reward_percent = max(0, self.get_loyalty_settings()["referral_reward_percent"])
            reward_rub = max(0, (int(order["amount_rub"] or 0) * reward_percent) // 100)
            conn.execute(
                """
                UPDATE tg_users
                SET referral_reward_granted = 1
                WHERE id = ?
                """,
                (int(order["tg_user_id"]),),
            )

            if inviter is None or int(inviter["is_partner"] or 0) == 1 or delivered_orders != 1 or reward_rub <= 0:
                return None
            conn.execute(
                """
                UPDATE tg_users
                SET referral_bonus_balance_rub = referral_bonus_balance_rub + ?,
                    total_referral_earned_rub = total_referral_earned_rub + ?,
                    referral_invites_count = referral_invites_count + 1
                WHERE id = ?
                """,
                (reward_rub, reward_rub, int(order["referred_by_user_id"])),
            )
            inviter = conn.execute(
                "SELECT telegram_id, referral_bonus_balance_rub FROM tg_users WHERE id = ?",
                (int(order["referred_by_user_id"]),),
            ).fetchone()
            if inviter is None:
                return None
            return {
                "inviter_telegram_id": int(inviter["telegram_id"]),
                "reward_rub": reward_rub,
                "reward_percent": reward_percent,
                "bonus_balance_rub": int(inviter["referral_bonus_balance_rub"]),
                "invited_telegram_id": int(order["telegram_id"]),
                "paid_amount_rub": int(order["amount_rub"] or 0),
            }

    def apply_partner_reward(self, order_id: int) -> dict[str, int | str] | None:
        with self.connect() as conn:
            order = conn.execute(
                """
                SELECT o.*,
                       u.telegram_id,
                       u.referred_by_user_id,
                       inviter.telegram_id AS partner_telegram_id,
                       inviter.username AS partner_username,
                       inviter.first_name AS partner_first_name,
                       inviter.is_partner AS inviter_is_partner,
                       inviter.partner_name,
                       inviter.partner_commission_percent
                FROM orders o
                JOIN tg_users u ON u.id = o.tg_user_id
                LEFT JOIN tg_users inviter ON inviter.id = u.referred_by_user_id
                WHERE o.id = ?
                """,
                (order_id,),
            ).fetchone()
            if order is None or str(order["status"]) != "delivered":
                return None
            if int(order["is_trial"] or 0) == 1 or int(order["amount_rub"] or 0) <= 0:
                return None
            if not order["referred_by_user_id"] or int(order["partner_reward_granted"] or 0) == 1:
                return None

            reward_percent = _coerce_percent(order["partner_commission_percent"], 0)
            inviter_is_partner = int(order["inviter_is_partner"] or 0) == 1
            reward_rub = 0
            if inviter_is_partner and reward_percent > 0:
                reward_rub = max(0, (int(order["amount_rub"] or 0) * reward_percent) // 100)

            conn.execute(
                """
                UPDATE orders
                SET partner_reward_rub = ?, partner_reward_granted = 1
                WHERE id = ?
                """,
                (reward_rub, order_id),
            )

            if not inviter_is_partner or reward_rub <= 0:
                return None

            conn.execute(
                """
                UPDATE tg_users
                SET partner_balance_rub = partner_balance_rub + ?,
                    total_partner_earned_rub = total_partner_earned_rub + ?
                WHERE id = ?
                """,
                (reward_rub, reward_rub, int(order["referred_by_user_id"])),
            )
            partner = conn.execute(
                """
                SELECT telegram_id, partner_balance_rub, total_partner_earned_rub,
                       partner_name, partner_commission_percent
                FROM tg_users
                WHERE id = ?
                """,
                (int(order["referred_by_user_id"]),),
            ).fetchone()
            if partner is None:
                return None
            partner_name = str(partner["partner_name"] or "").strip()
            if not partner_name:
                partner_name = str(order["partner_username"] or order["partner_first_name"] or order["partner_telegram_id"] or "")
            return {
                "partner_telegram_id": int(partner["telegram_id"]),
                "reward_rub": reward_rub,
                "reward_percent": int(partner["partner_commission_percent"] or 0),
                "partner_balance_rub": int(partner["partner_balance_rub"] or 0),
                "total_partner_earned_rub": int(partner["total_partner_earned_rub"] or 0),
                "customer_telegram_id": int(order["telegram_id"]),
                "paid_amount_rub": int(order["amount_rub"] or 0),
                "partner_name": partner_name,
            }

    def get_order(self, order_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT o.*,
                       COALESCE(p.name, 'Архивный тариф') AS plan_name,
                       COALESCE(p.duration_days, 0) AS duration_days,
                       COALESCE(p.traffic_gb, 0) AS traffic_gb,
                       COALESCE(p.description, '') AS description,
                       COALESCE(p.inbound_ids, '[]') AS inbound_ids,
                       u.telegram_id, u.username, u.first_name, u.last_name, u.referral_code
                FROM orders o
                LEFT JOIN plans p ON p.id = o.plan_id
                JOIN tg_users u ON u.id = o.tg_user_id
                WHERE o.id = ?
                """,
                (order_id,),
            ).fetchone()

    def get_order_by_public_id(self, public_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT o.*,
                       COALESCE(p.name, 'Архивный тариф') AS plan_name,
                       COALESCE(p.duration_days, 0) AS duration_days,
                       COALESCE(p.traffic_gb, 0) AS traffic_gb,
                       COALESCE(p.description, '') AS description,
                       COALESCE(p.inbound_ids, '[]') AS inbound_ids,
                       u.telegram_id, u.username, u.first_name, u.last_name, u.referral_code
                FROM orders o
                LEFT JOIN plans p ON p.id = o.plan_id
                JOIN tg_users u ON u.id = o.tg_user_id
                WHERE o.public_id = ?
                """,
                (public_id,),
            ).fetchone()

    def list_orders(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT o.*,
                       COALESCE(p.name, 'Архивный тариф') AS plan_name,
                       COALESCE(p.duration_days, 0) AS duration_days,
                       COALESCE(p.traffic_gb, 0) AS traffic_gb,
                       u.telegram_id, u.username, u.first_name, u.last_name
                FROM orders o
                LEFT JOIN plans p ON p.id = o.plan_id
                JOIN tg_users u ON u.id = o.tg_user_id
                ORDER BY o.id DESC
                """
            ).fetchall()

    def list_user_orders(self, tg_user_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT o.*, COALESCE(p.name, 'Архивный тариф') AS plan_name
                FROM orders o
                LEFT JOIN plans p ON p.id = o.plan_id
                WHERE o.tg_user_id = ?
                ORDER BY o.id DESC
                """,
                (tg_user_id,),
            ).fetchall()

    def list_users(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT u.*,
                       inviter.telegram_id AS inviter_telegram_id,
                       inviter.username AS inviter_username,
                       (
                           SELECT COUNT(*)
                           FROM orders o
                           WHERE o.tg_user_id = u.id
                       ) AS orders_count,
                       (
                           SELECT COUNT(*)
                           FROM orders o
                           WHERE o.tg_user_id = u.id
                             AND o.status = 'delivered'
                             AND COALESCE(o.is_trial, 0) = 0
                       ) AS delivered_orders_count,
                       (
                           SELECT COUNT(*)
                           FROM tg_users child
                           WHERE child.referred_by_user_id = u.id
                       ) AS referred_users_count,
                       (
                           SELECT COUNT(*)
                           FROM orders o
                           JOIN tg_users child ON child.id = o.tg_user_id
                           WHERE child.referred_by_user_id = u.id
                             AND o.status = 'delivered'
                             AND COALESCE(o.is_trial, 0) = 0
                       ) AS referred_paid_orders_count,
                       (
                           SELECT COALESCE(SUM(o.amount_rub), 0)
                           FROM orders o
                           JOIN tg_users child ON child.id = o.tg_user_id
                           WHERE child.referred_by_user_id = u.id
                             AND o.status = 'delivered'
                             AND COALESCE(o.is_trial, 0) = 0
                       ) AS referred_paid_revenue_rub
                FROM tg_users u
                LEFT JOIN tg_users inviter ON inviter.id = u.referred_by_user_id
                ORDER BY u.id DESC
                """
            ).fetchall()

    def list_partner_orders(self, partner_user_id: int, *, limit: int = 25) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT o.public_id,
                       o.status,
                       o.amount_rub,
                       o.base_amount_rub,
                       o.discount_rub,
                       o.partner_reward_rub,
                       o.payment_status,
                       o.created_at,
                       o.updated_at,
                       o.is_trial,
                       COALESCE(p.name, 'РђСЂС…РёРІРЅС‹Р№ С‚Р°СЂРёС„') AS plan_name,
                       customer.telegram_id AS customer_telegram_id,
                       customer.username AS customer_username,
                       customer.first_name AS customer_first_name
                FROM orders o
                JOIN tg_users customer ON customer.id = o.tg_user_id
                LEFT JOIN plans p ON p.id = o.plan_id
                WHERE customer.referred_by_user_id = ?
                ORDER BY o.id DESC
                LIMIT ?
                """,
                (partner_user_id, max(1, int(limit))),
            ).fetchall()

    def list_partner_payouts(self, partner_user_id: int, *, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT id, amount_rub, note, created_at
                FROM partner_payouts
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (partner_user_id, max(1, int(limit))),
            ).fetchall()

    def list_partner_withdraw_requests(self, partner_user_id: int, *, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT id, amount_rub, payout_details, status, created_at, processed_at, note
                FROM partner_withdraw_requests
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (partner_user_id, max(1, int(limit))),
            ).fetchall()

    def get_pending_partner_withdraw_request(self, partner_user_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT id, amount_rub, payout_details, status, created_at, processed_at, note
                FROM partner_withdraw_requests
                WHERE user_id = ? AND status = 'pending'
                ORDER BY id DESC
                LIMIT 1
                """,
                (partner_user_id,),
            ).fetchone()

    def list_all_partner_withdraw_requests(self, *, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT r.id, r.user_id, r.amount_rub, r.payout_details, r.status, r.created_at, r.processed_at, r.note,
                       u.telegram_id, u.username, u.first_name, u.partner_name
                FROM partner_withdraw_requests r
                JOIN tg_users u ON u.id = r.user_id
                ORDER BY CASE WHEN r.status = 'pending' THEN 0 ELSE 1 END, r.id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()

    def create_partner_withdraw_request(
        self,
        partner_user_id: int,
        *,
        min_amount_rub: int = 1000,
        payout_details: str = "",
    ) -> sqlite3.Row:
        with self.connect() as conn:
            user = conn.execute(
                """
                SELECT id, telegram_id, username, first_name, is_partner, partner_balance_rub
                FROM tg_users
                WHERE id = ?
                """,
                (partner_user_id,),
            ).fetchone()
            if user is None or int(user["is_partner"] or 0) != 1:
                raise ValueError("Partner access is unavailable")

            pending = conn.execute(
                """
                SELECT id, amount_rub, status, created_at, processed_at, note
                FROM partner_withdraw_requests
                WHERE user_id = ? AND status = 'pending'
                ORDER BY id DESC
                LIMIT 1
                """,
                (partner_user_id,),
            ).fetchone()
            if pending is not None:
                raise ValueError("Withdraw request is already pending")

            balance_rub = int(user["partner_balance_rub"] or 0)
            if balance_rub < max(1, int(min_amount_rub)):
                raise ValueError("Balance is too low")
            details = payout_details.strip()
            if not details:
                raise ValueError("Payout details are required")

            created_at = utc_now_iso()
            conn.execute(
                """
                UPDATE tg_users
                SET partner_balance_rub = partner_balance_rub - ?
                WHERE id = ?
                """,
                (balance_rub, partner_user_id),
            )
            conn.execute(
                """
                INSERT INTO partner_withdraw_requests(
                    user_id, amount_rub, payout_details, balance_reserved, status, created_at, processed_at, note
                )
                VALUES (?, ?, ?, 1, 'pending', ?, NULL, '')
                """,
                (partner_user_id, balance_rub, details, created_at),
            )
            request_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            row = conn.execute(
                """
                SELECT r.id, r.amount_rub, r.payout_details, r.balance_reserved, r.status, r.created_at, r.processed_at, r.note,
                       u.telegram_id, u.username, u.first_name, u.partner_name
                FROM partner_withdraw_requests r
                JOIN tg_users u ON u.id = r.user_id
                WHERE r.id = ?
                """,
                (request_id,),
            ).fetchone()
            assert row is not None
            return row

    def get_user_profile(self, user_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT u.*,
                       inviter.telegram_id AS inviter_telegram_id,
                       inviter.username AS inviter_username,
                       (
                           SELECT COUNT(*)
                           FROM orders o
                           WHERE o.tg_user_id = u.id
                       ) AS orders_count,
                       (
                           SELECT COUNT(*)
                           FROM orders o
                           WHERE o.tg_user_id = u.id
                             AND o.status = 'delivered'
                             AND COALESCE(o.is_trial, 0) = 0
                       ) AS delivered_orders_count,
                       (
                           SELECT COUNT(*)
                           FROM tg_users child
                           WHERE child.referred_by_user_id = u.id
                       ) AS referred_users_count,
                       (
                           SELECT COUNT(*)
                           FROM orders o
                           JOIN tg_users child ON child.id = o.tg_user_id
                           WHERE child.referred_by_user_id = u.id
                             AND o.status = 'delivered'
                             AND COALESCE(o.is_trial, 0) = 0
                       ) AS referred_paid_orders_count,
                       (
                           SELECT COALESCE(SUM(o.amount_rub), 0)
                           FROM orders o
                           JOIN tg_users child ON child.id = o.tg_user_id
                           WHERE child.referred_by_user_id = u.id
                             AND o.status = 'delivered'
                             AND COALESCE(o.is_trial, 0) = 0
                       ) AS referred_paid_revenue_rub
                FROM tg_users u
                LEFT JOIN tg_users inviter ON inviter.id = u.referred_by_user_id
                WHERE u.id = ?
                """,
                (user_id,),
            ).fetchone()

    def get_user_profile_by_telegram_id(self, telegram_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT u.*,
                       inviter.telegram_id AS inviter_telegram_id,
                       inviter.username AS inviter_username,
                       (
                           SELECT COUNT(*)
                           FROM orders o
                           WHERE o.tg_user_id = u.id
                       ) AS orders_count,
                       (
                           SELECT COUNT(*)
                           FROM orders o
                           WHERE o.tg_user_id = u.id
                             AND o.status = 'delivered'
                             AND COALESCE(o.is_trial, 0) = 0
                       ) AS delivered_orders_count,
                       (
                           SELECT COUNT(*)
                           FROM tg_users child
                           WHERE child.referred_by_user_id = u.id
                       ) AS referred_users_count,
                       (
                           SELECT COUNT(*)
                           FROM orders o
                           JOIN tg_users child ON child.id = o.tg_user_id
                           WHERE child.referred_by_user_id = u.id
                             AND o.status = 'delivered'
                             AND COALESCE(o.is_trial, 0) = 0
                       ) AS referred_paid_orders_count,
                       (
                           SELECT COALESCE(SUM(o.amount_rub), 0)
                           FROM orders o
                           JOIN tg_users child ON child.id = o.tg_user_id
                           WHERE child.referred_by_user_id = u.id
                             AND o.status = 'delivered'
                             AND COALESCE(o.is_trial, 0) = 0
                       ) AS referred_paid_revenue_rub
                FROM tg_users u
                LEFT JOIN tg_users inviter ON inviter.id = u.referred_by_user_id
                WHERE u.telegram_id = ?
                """,
                (telegram_id,),
            ).fetchone()


def plan_inbound_ids(row: sqlite3.Row) -> list[int]:
    return _from_json(row["inbound_ids"])


def order_links(row: sqlite3.Row) -> list[str]:
    raw = row["xui_links_json"]
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in value if isinstance(item, str)]
