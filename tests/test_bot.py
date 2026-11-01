import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bot import onboarding, onboarding_review
from app.config import Settings


def settings():
    return Settings(
        database_url="unused",
        signing_key="x" * 32,
        bot_api_key="y" * 32,
        bot_token="unused",
        organizer_ids=frozenset(),
        public_url="https://example.com/guestpass",
    )


def test_onboarding_fits_telegram_caption_and_has_review_button():
    message = SimpleNamespace(answer_photo=AsyncMock())
    asyncio.run(onboarding(message, settings()))
    sent = message.answer_photo.call_args
    assert len(sent.kwargs["caption"]) <= 1024
    assert "/review" in sent.kwargs["caption"]
    assert sent.kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "onboarding:review"


def test_review_callback_uses_person_not_bot_message_author():
    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"), from_user=SimpleNamespace(id=123456), answer=AsyncMock(), answer_photo=AsyncMock()
    )
    query = SimpleNamespace(message=message, from_user=SimpleNamespace(id=999), answer=AsyncMock())
    backend = SimpleNamespace(request=AsyncMock(return_value={"guests": []}))
    asyncio.run(onboarding_review(query, backend, settings()))
    backend.request.assert_awaited_once_with("POST", "/internal/review", json={"actor_id": 999})


def test_new_participant_is_asked_for_name_before_registration():
    from app.bot import Registration, start

    message = SimpleNamespace(
        chat=SimpleNamespace(type="private"), text="/start", from_user=SimpleNamespace(id=888), answer=AsyncMock(), answer_photo=AsyncMock()
    )
    state = SimpleNamespace(clear=AsyncMock(), set_state=AsyncMock())
    backend = SimpleNamespace(request=AsyncMock(return_value={"registered": False, "name": None}))
    asyncio.run(start(message, backend, settings(), state))
    state.set_state.assert_awaited_once_with(Registration.name)
    backend.request.assert_awaited_once_with("GET", "/internal/registration/888")


def test_full_name_validation():
    from app.bot import valid_name

    assert valid_name("Петров Иван")
    assert valid_name("О’Коннор Анна-Мария")
    for value in ["Иван", "GP1.signed-token", "Петров 123", "😀 😀", "a" * 101]:
        assert not valid_name(value)
