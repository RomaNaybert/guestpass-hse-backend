import time
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import Settings
from app.security import InvalidToken, PassClaims, issue_pass, verify_pass


class GuestService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool = ConnectionPool(settings.database_url, min_size=1, max_size=8, kwargs={"row_factory": dict_row}, open=False)
        self.event_id = None

    def open(self):
        self.pool.open(wait=True, timeout=30)
        with self.pool.connection() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(76481025)")
            conn.execute((Path(__file__).with_name("schema.sql")).read_text())
            row = conn.execute(
                "INSERT INTO events(id,slug,name) VALUES(%s,%s,%s) ON CONFLICT(slug) DO UPDATE SET name=EXCLUDED.name RETURNING id",
                (uuid4(), self.settings.event_slug, self.settings.event_name),
            ).fetchone()
            self.event_id = row["id"]

    def register(self, telegram_id: int, name: str):
        with self.pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO guests(id,event_id,telegram_id,name,pass_nonce,pass_expires)
                   VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id,telegram_id)
                   DO UPDATE SET name=EXCLUDED.name,
                   pass_nonce=CASE WHEN guests.pass_expires<=extract(epoch FROM now()) THEN EXCLUDED.pass_nonce ELSE guests.pass_nonce END,
                   pass_expires=CASE WHEN guests.pass_expires<=extract(epoch FROM now()) THEN EXCLUDED.pass_expires ELSE guests.pass_expires END
                   RETURNING *""",
                (uuid4(), self.event_id, telegram_id, name, uuid4(), int(time.time()) + self.settings.pass_ttl_days * 86400),
            ).fetchone()
        return self.card(row["id"])

    def registration_for(self, telegram_id: int):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT name FROM guests WHERE event_id=%s AND telegram_id=%s", (self.event_id, telegram_id)).fetchone()
        return {"registered": bool(row), "name": row["name"] if row else None}

    def legacy_guest(self, guest_id: UUID):
        # Old private review tickets contain only synthetic guests. Resolve them to the shared demo guests.
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT guests.* FROM guests JOIN events ON events.id=guests.event_id WHERE guests.id=%s AND events.slug LIKE %s AND guests.telegram_id IN (1,2)",
                (guest_id, f"review-{self.event_id}-%"),
            ).fetchone()
            if not row:
                return None
            canonical = conn.execute(
                "SELECT id FROM guests WHERE event_id=%s AND telegram_id=%s", (self.event_id, row["telegram_id"])
            ).fetchone()
        return (row, canonical["id"]) if canonical else None

    def scan_for(self, actor_id: int, token: str):
        scoped = self.context_for(actor_id)
        card = scoped.scan(token)
        return {**card, "review_mode": self.profile_for(actor_id)["review_mode"]}

    def check_in_for(self, guest_id: UUID, actor_id: int):
        scoped = self.context_for(actor_id)
        legacy = self.legacy_guest(guest_id) if self.settings.review_enabled else None
        result = scoped.check_in(legacy[1] if legacy else guest_id, actor_id)
        result["guest"]["review_mode"] = self.profile_for(actor_id)["review_mode"]
        return result

    def pass_for(self, telegram_id: int):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM guests WHERE event_id=%s AND telegram_id=%s", (self.event_id, telegram_id)).fetchone()
        if not row:
            raise LookupError("Сначала отправьте /start")
        if row["pass_expires"] <= int(time.time()):
            raise InvalidToken("Пропуск истёк. Отправьте /start, чтобы получить новый")
        return issue_pass(PassClaims(self.event_id, row["id"], row["pass_nonce"], row["pass_expires"]), self.settings.signing_key)

    def scan(self, token: str):
        claims = verify_pass(token.strip(), self.settings.signing_key)
        if claims.event_id != self.event_id:
            legacy = self.legacy_guest(claims.guest_id) if self.settings.review_enabled else None
            if not legacy or legacy[0]["event_id"] != claims.event_id:
                raise InvalidToken("Пропуск другого мероприятия")
            if legacy[0]["pass_nonce"] != claims.nonce or legacy[0]["pass_expires"] != claims.expires:
                raise InvalidToken("Пропуск отозван или не найден")
            return self.card(legacy[1])
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT pass_nonce,pass_expires FROM guests WHERE id=%s AND event_id=%s", (claims.guest_id, self.event_id)
            ).fetchone()
        if not row or row["pass_nonce"] != claims.nonce or row["pass_expires"] != claims.expires:
            raise InvalidToken("Пропуск отозван или не найден")
        return self.card(claims.guest_id)

    def card(self, guest_id: UUID):
        with self.pool.connection() as conn:
            guest = conn.execute("SELECT id,name,category FROM guests WHERE event_id=%s AND id=%s", (self.event_id, guest_id)).fetchone()
            if not guest:
                raise LookupError("Гость не найден")
            entries = conn.execute(
                """SELECT amount,reason,actor_telegram_id,created_at,kind FROM point_entries
                   WHERE event_id=%s AND guest_id=%s ORDER BY created_at DESC""",
                (self.event_id, guest_id),
            ).fetchall()
        return {
            **guest,
            "event_name": self.settings.event_name,
            "points": sum(x["amount"] for x in entries),
            "checked_in": any(x["kind"] == "check_in" for x in entries),
            "history": entries,
        }

    def check_in(self, guest_id: UUID, actor_id: int):
        if actor_id not in self.settings.organizer_ids:
            raise PermissionError("Нет доступа организатора")
        with self.pool.connection() as conn:
            guest = conn.execute("SELECT id FROM guests WHERE event_id=%s AND id=%s", (self.event_id, guest_id)).fetchone()
            if not guest:
                raise LookupError("Гость не найден")
            inserted = conn.execute(
                """INSERT INTO point_entries(id,event_id,guest_id,actor_telegram_id,amount,kind,reason)
                   VALUES(%s,%s,%s,%s,%s,'check_in','Приход на мероприятие')
                   ON CONFLICT(event_id,guest_id,kind) DO NOTHING RETURNING id""",
                (uuid4(), self.event_id, guest_id, actor_id, self.settings.check_in_points),
            ).fetchone()
        # Read after commit: concurrent callers see the completed entry.
        return {"awarded": inserted is not None, "guest": self.card(guest_id)}

    def stats(self):
        with self.pool.connection() as conn:
            row = conn.execute(
                """SELECT
                (SELECT count(*) FROM guests WHERE event_id=%s) AS registered,
                (SELECT count(*) FROM point_entries WHERE event_id=%s AND kind='check_in') AS checked_in,
                (SELECT coalesce(sum(amount),0) FROM point_entries WHERE event_id=%s) AS points""",
                (self.event_id, self.event_id, self.event_id),
            ).fetchone()
        return row

    def scoped(self, event_id: UUID, event_name: str, actor_id: int):
        """Share a pool, but constrain every query and permission to one event."""
        scoped = object.__new__(GuestService)
        scoped.pool = self.pool
        scoped.event_id = event_id
        scoped.settings = replace(self.settings, event_name=event_name, organizer_ids=frozenset({actor_id}))
        return scoped

    def context_for(self, actor_id: int):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT active FROM review_spaces WHERE actor_telegram_id=%s", (actor_id,)).fetchone()
        if self.settings.review_enabled and row and row["active"]:
            return self.scoped(self.event_id, self.settings.event_name, actor_id)
        if actor_id in self.settings.organizer_ids:
            return self
        raise PermissionError("Нет доступа организатора. Для проверки отправьте боту /review")

    def profile_for(self, actor_id: int):
        self.context_for(actor_id)
        with self.pool.connection() as conn:
            row = conn.execute("SELECT active FROM review_spaces WHERE actor_telegram_id=%s", (actor_id,)).fetchone()
        return {
            "organizer_id": actor_id,
            "review_mode": bool(self.settings.review_enabled and row and row["active"]),
            "event_name": self.settings.event_name,
        }

    def start_review(self, actor_id: int):
        if actor_id <= 0 or not self.settings.review_enabled:
            raise PermissionError("Публичная проверка отключена. Доступ есть только у организаторов")
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO review_spaces(actor_telegram_id,event_id,active) VALUES(%s,%s,true) ON CONFLICT(actor_telegram_id) DO UPDATE SET active=true,event_id=EXCLUDED.event_id",
                (actor_id, self.event_id),
            )
        guests = []
        for test_id, name in [(1, "Анна Кузнецова"), (2, "Михаил Соколов")]:
            card = self.register(test_id, name)
            with self.pool.connection() as conn:
                conn.execute("UPDATE guests SET category='Тестовый гость' WHERE event_id=%s AND id=%s", (self.event_id, card["id"]))
            guests.append({"name": name, "token": self.pass_for(test_id)})
        return {"event_name": self.settings.event_name, "guests": guests}

    def leave_review(self, actor_id: int):
        with self.pool.connection() as conn:
            conn.execute("UPDATE review_spaces SET active=false WHERE actor_telegram_id=%s", (actor_id,))
