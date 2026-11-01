import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.security import InvalidToken, issue_session
from app.service import GuestService

pytestmark = pytest.mark.integration
DB = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def settings():
    if not DB:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    return Settings(
        database_url=DB,
        signing_key="integration-secret-signing-key-000000",
        bot_api_key="integration-internal-api-key-00000000",
        bot_token="123456:TestBotToken",
        organizer_ids=frozenset({123}),
        event_slug="test-" + str(uuid4()),
    )


@pytest.fixture
def service(settings):
    svc = GuestService(settings)
    svc.open()
    yield svc
    with svc.pool.connection() as conn:
        review_events = conn.execute("SELECT id FROM events WHERE slug LIKE %s", (f"review-{svc.event_id}-%",)).fetchall()
        for event in review_events:
            conn.execute("DELETE FROM review_spaces WHERE event_id=%s", (event["id"],))
            conn.execute("DELETE FROM point_entries WHERE event_id=%s", (event["id"],))
            conn.execute("DELETE FROM guests WHERE event_id=%s", (event["id"],))
            conn.execute("DELETE FROM events WHERE id=%s", (event["id"],))
        conn.execute("DELETE FROM point_entries WHERE event_id=%s", (svc.event_id,))
        conn.execute("DELETE FROM guests WHERE event_id=%s", (svc.event_id,))
        conn.execute("DELETE FROM events WHERE id=%s", (svc.event_id,))
    svc.pool.close()


def test_register_is_idempotent_and_pass_is_personal(service):
    first = service.register(777, "Анна")
    second = service.register(777, "Анна Кузнецова")
    assert first["id"] == second["id"]
    assert service.stats()["registered"] == 1
    token = service.pass_for(777)
    assert service.scan(token)["name"] == "Анна Кузнецова"


def test_concurrent_check_in_awards_once(service):
    guest = service.register(777, "Анна")
    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(lambda _: service.check_in(guest["id"], 123), range(24)))
    assert sum(result["awarded"] for result in results) == 1
    card = service.card(guest["id"])
    assert card["points"] == 10
    assert card["checked_in"] is True
    assert len(card["history"]) == 1
    assert card["history"][0]["actor_telegram_id"] == 123
    assert card["history"][0]["reason"] == "Приход на мероприятие"
    assert card["history"][0]["created_at"]


def test_unauthorized_actor_cannot_award(service):
    guest = service.register(777, "Анна")
    with pytest.raises(PermissionError):
        service.check_in(guest["id"], 999)
    assert service.card(guest["id"])["points"] == 0


def test_scanning_does_not_award(service):
    guest = service.register(777, "Анна")
    token = service.pass_for(777)
    for _ in range(3):
        assert service.scan(token)["checked_in"] is False
    assert service.card(guest["id"])["points"] == 0


def test_other_event_pass_rejected(service, settings):
    service.register(777, "Анна")
    token = service.pass_for(777)
    other = GuestService(replace(settings, event_slug="test-" + str(uuid4())))
    other.open()
    try:
        with pytest.raises(InvalidToken, match="другого мероприятия"):
            other.scan(token)
    finally:
        with other.pool.connection() as conn:
            conn.execute("DELETE FROM events WHERE id=%s", (other.event_id,))
        other.pool.close()


def test_api_auth_and_checkin(settings):
    app = create_app(settings)
    headers = {"Authorization": "Bearer " + settings.bot_api_key}
    with TestClient(app) as client:
        assert client.post("/internal/register", json={"telegram_id": 777, "name": "Анна"}).status_code == 401
        registered = client.post("/internal/register", headers=headers, json={"telegram_id": 777, "name": "Анна"}).json()
        guest_id = registered["id"]
        token = client.get("/internal/pass/777", headers=headers).json()["token"]
        assert client.post("/internal/scan", headers=headers, json={"actor_id": 999, "token": token}).status_code == 403
        assert client.post("/api/scan", json={"token": token}).status_code == 403
        client.cookies.set("guestpass_session", issue_session(123, settings.signing_key))
        web_headers = {"X-Guestpass-Request": "1"}
        assert client.post("/api/scan", headers=web_headers, json={"token": token}).status_code == 200
        result = client.post(f"/api/guests/{guest_id}/check-in", headers=web_headers, json={}).json()
        assert result["awarded"] is True
        again = client.post(f"/api/guests/{guest_id}/check-in", headers=web_headers, json={}).json()
        assert again["awarded"] is False
        assert again["guest"]["points"] == 10
        assert client.post("/api/scan", headers={**web_headers, "Origin": "https://evil.example"}, json={"token": token}).status_code == 403
        assert client.post(f"/api/guests/{UUID(int=0)}/check-in", headers=web_headers, json={}).status_code == 404
        service = app.state.service
        with service.pool.connection() as conn:
            conn.execute("DELETE FROM point_entries WHERE event_id=%s", (service.event_id,))
            conn.execute("DELETE FROM guests WHERE event_id=%s", (service.event_id,))
            conn.execute("DELETE FROM events WHERE id=%s", (service.event_id,))


def test_reviewers_are_isolated_from_each_other_and_real_guests(service):
    main_guest = service.register(777, "Настоящий гость")
    main_token = service.pass_for(777)
    first = service.start_review(999)
    second = service.start_review(888)
    first_scope = service.context_for(999)
    second_scope = service.context_for(888)
    card = first_scope.scan(first["guests"][0]["token"])
    assert first_scope.check_in(card["id"], 999)["awarded"] is True
    assert first_scope.check_in(card["id"], 999)["awarded"] is False
    with pytest.raises(InvalidToken):
        first_scope.scan(second["guests"][0]["token"])
    with pytest.raises(InvalidToken):
        first_scope.scan(main_token)
    with pytest.raises(LookupError):
        first_scope.check_in(main_guest["id"], 999)
    with pytest.raises(LookupError):
        second_scope.check_in(card["id"], 888)
    assert service.card(main_guest["id"])["points"] == 0
    assert service.stats()["points"] == 0
    assert second_scope.stats()["points"] == 0
    assert first_scope.stats()["points"] == 10


def test_review_permissions_are_persistent_and_can_be_left(service, settings):
    with pytest.raises(PermissionError):
        service.context_for(999)
    service.start_review(999)
    reopened = GuestService(settings)
    reopened.open()
    try:
        assert reopened.profile_for(999)["review_mode"] is True
        reopened.leave_review(999)
        with pytest.raises(PermissionError):
            reopened.context_for(999)
        assert reopened.context_for(123).event_id == service.event_id
    finally:
        reopened.pool.close()


def test_reviewer_photo_decoding_and_award_via_real_api(settings):
    import base64
    from app.qr import make_qr

    app = create_app(settings)
    headers = {"Authorization": "Bearer " + settings.bot_api_key}
    with TestClient(app) as client:
        review = client.post("/internal/review", headers=headers, json={"actor_id": 999}).json()
        token = review["guests"][0]["token"]
        client.cookies.set("guestpass_session", issue_session(999, settings.signing_key))
        web = {"X-Guestpass-Request": "1"}
        decoded = client.post("/api/scan-photo", headers=web, json={"image_base64": base64.b64encode(make_qr(token)).decode()})
        assert decoded.status_code == 200
        guest = decoded.json()
        result = client.post(f"/api/guests/{guest['id']}/check-in", headers=web, json={}).json()
        assert result["guest"]["points"] == 10
        assert result["guest"]["history"][0]["actor_telegram_id"] == 999
        assert client.get("/api/me").json()["review_mode"] is True
        scoped = app.state.service.context_for(999)
        with scoped.pool.connection() as conn:
            conn.execute("DELETE FROM review_spaces WHERE event_id=%s", (scoped.event_id,))
            conn.execute("DELETE FROM point_entries WHERE event_id=%s", (scoped.event_id,))
            conn.execute("DELETE FROM guests WHERE event_id=%s", (scoped.event_id,))
            conn.execute("DELETE FROM events WHERE id IN (%s,%s)", (scoped.event_id, app.state.service.event_id))
