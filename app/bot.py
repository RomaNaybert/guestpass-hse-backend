import asyncio
import html
import logging
from io import BytesIO

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from app.config import Settings
from app.qr import MAX_IMAGE_BYTES, make_qr, read_qr
from app.security import InvalidToken

router = Router()


class ApiError(ValueError):
    pass


class Backend:
    def __init__(self, settings):
        self.client = httpx.AsyncClient(base_url=settings.api_url, headers={"Authorization": "Bearer " + settings.bot_api_key}, timeout=15)

    async def request(self, method, path, **kwargs):
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise ApiError("Сервис временно недоступен. Попробуйте ещё раз") from exc
        if response.is_error:
            try:
                detail = response.json().get("detail", "Не удалось выполнить запрос")
            except ValueError:
                detail = "Сервис временно недоступен. Попробуйте ещё раз"
            raise ApiError(detail if isinstance(detail, str) else "Проверьте формат данных")
        return response.json()


def card_text(card):
    status = "Уже отмечен" if card["checked_in"] else "Ещё не отмечен"
    text = (
        f"<b>{html.escape(card['name'])}</b>\nКатегория: {html.escape(card['category'])}\nБаллы: <b>{card['points']}</b>\nПриход: {status}"
    )
    for entry in card["history"][:5]:
        text += f"\n\n+{entry['amount']} · {html.escape(entry['reason'])}\n{html.escape(entry['created_at'])} · организатор {entry['actor_telegram_id']}"
    return text


def card_buttons(card, settings):
    if card["checked_in"]:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=f"Отметить приход · +{settings.check_in_points}", callback_data=f"check:{card['id']}")]]
    )


async def allowed(message, backend):
    if message.chat.type != "private" or not message.from_user:
        return False
    try:
        await backend.request("GET", f"/internal/access/{message.from_user.id}")
        return True
    except ApiError:
        return False


@router.message(CommandStart())
async def start(message: Message, backend: Backend, settings: Settings):
    if message.chat.type != "private":
        return
    if message.text and message.text.split(maxsplit=1)[-1] == "review":
        await review(message, backend, settings)
        return
    try:
        await backend.request(
            "POST", "/internal/register", json={"telegram_id": message.from_user.id, "name": message.from_user.full_name[:100]}
        )
        await message.answer(
            f"Вы зарегистрированы на «{settings.event_name}».\n\n/qr — персональный пропуск\n/id — ваш Telegram ID\n/organizer — панель организатора\n/review — проверить весь сценарий на личном тестовом стенде\n\nПокажите QR на входе. Персональный пропуск не стоит пересылать другим людям."
        )
        await send_qr(message, backend, settings)
    except ApiError as exc:
        await message.answer(str(exc))


@router.message(Command("id"))
async def identity(message: Message):
    if message.chat.type == "private":
        await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.message(Command("qr"))
async def send_qr(message: Message, backend: Backend, settings: Settings):
    if message.chat.type != "private":
        return
    try:
        data = await backend.request("GET", f"/internal/pass/{message.from_user.id}")
        png = await asyncio.to_thread(make_qr, data["token"])
        await message.answer_photo(
            BufferedInputFile(png, filename="guest-pass.png"),
            caption=f"{settings.event_name}\nВаш персональный пропуск. Покажите QR на входе.",
        )
        # Copyable fallback is sent separately, without exposing it to group chats.
        await message.answer(f"Код на случай проблем с камерой:\n<code>{html.escape(data['token'])}</code>", parse_mode="HTML")
    except (ApiError, InvalidToken) as exc:
        await message.answer(str(exc))


@router.message(Command("organizer"))
async def organizer(message: Message, backend: Backend, settings: Settings):
    if not await allowed(message, backend):
        await message.answer("Этот раздел доступен организаторам. Для проверки задания отправьте /review — получите личный тестовый стенд.")
        return
    keyboard = None
    if settings.public_url.startswith("https://"):
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Открыть сканер", web_app=WebAppInfo(url=settings.public_url + "/"))]]
        )
    await message.answer(
        "Отправьте фото QR, перешлите картинку или вставьте код GP1.…\n\nСначала откроется карточка гостя. Баллы начисляются только после нажатия кнопки «Отметить приход». Повторное нажатие их не добавит.",
        reply_markup=keyboard,
    )


@router.message(Command("review"))
async def review(message: Message, backend: Backend, settings: Settings):
    if message.chat.type != "private":
        return
    try:
        result = await backend.request("POST", "/internal/review", json={"actor_id": message.from_user.id})
        await message.answer(
            "Открыт ваш личный тестовый стенд. Ниже — два QR вымышленных гостей. Перешлите любое из этих фото обратно боту или скопируйте код: появится карточка и кнопка начисления. Повторите действие, чтобы проверить защиту от повторных баллов.\n\n/organizer — открыть веб-сканер\n/event — выйти из тестового режима\n\nНачисления сохраняются только на вашем стенде. Другие проверяющие и реальные гости от них не зависят."
        )
        for guest in result["guests"]:
            png = await asyncio.to_thread(make_qr, guest["token"])
            await message.answer_photo(
                BufferedInputFile(png, filename="review-qr.png"),
                caption=f"Тестовый гость: {guest['name']}\nПерешлите это фото обратно в этот чат.",
            )
            await message.answer(f"<code>{html.escape(guest['token'])}</code>", parse_mode="HTML")
        await organizer(message, backend, settings)
    except ApiError as exc:
        await message.answer(str(exc))


@router.message(Command("event"))
async def leave_review(message: Message, backend: Backend):
    if message.chat.type == "private":
        try:
            await backend.request("POST", "/internal/leave-review", json={"actor_id": message.from_user.id})
            await message.answer("Тестовый режим выключен. /qr — ваш пропуск на основную встречу; /review — вернуться к проверке.")
        except ApiError as exc:
            await message.answer(str(exc))


async def show_card(message, token, backend, settings):
    card = await backend.request("POST", "/internal/scan", json={"actor_id": message.from_user.id, "token": token})
    await message.answer(card_text(card), parse_mode="HTML", reply_markup=card_buttons(card, settings))


@router.message(F.photo | F.document)
async def photo(message: Message, bot: Bot, backend: Backend, settings: Settings):
    if not await allowed(message, backend):
        await message.answer("Распознавать пропуска могут только организаторы.")
        return
    media = message.photo[-1] if message.photo else message.document
    if media.file_size and media.file_size > MAX_IMAGE_BYTES:
        await message.answer("Отправьте картинку до 10 МБ.")
        return
    try:
        buffer = BytesIO()
        await bot.download(media, destination=buffer)
        token = await asyncio.to_thread(read_qr, buffer.getvalue())
        await show_card(message, token, backend, settings)
    except (InvalidToken, ApiError) as exc:
        await message.answer(str(exc))


@router.message(F.text)
async def code(message: Message, backend: Backend, settings: Settings):
    if not await allowed(message, backend):
        if message.chat.type == "private":
            await message.answer("Чтобы получить свой пропуск, отправьте /start или /qr.")
        return
    token = message.text.strip()
    if not token.startswith("GP1."):
        await message.answer("Отправьте фото QR или код, начинающийся с GP1.")
        return
    try:
        await show_card(message, token, backend, settings)
    except (InvalidToken, ApiError) as exc:
        await message.answer(str(exc))


@router.callback_query(F.data.startswith("check:"))
async def check_in(query: CallbackQuery, backend: Backend, settings: Settings):
    if not query.message or query.message.chat.type != "private":
        await query.answer("Нет доступа", show_alert=True)
        return
    try:
        result = await backend.request(
            "POST", f"/internal/guests/{query.data.split(':', 1)[1]}/check-in", json={"actor_id": query.from_user.id}
        )
        await query.answer("Приход отмечен" if result["awarded"] else "Приход уже был отмечен")
        await query.message.edit_text(card_text(result["guest"]), parse_mode="HTML")
    except ApiError as exc:
        await query.answer(str(exc)[:180], show_alert=True)


async def main():
    settings = Settings.from_env()
    if not settings.bot_token:
        raise ValueError("Set BOT_TOKEN from BotFather in .env")
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    bot = Bot(settings.bot_token)
    backend = Backend(settings)
    dispatcher = Dispatcher(settings=settings, backend=backend)
    dispatcher.include_router(router)
    try:
        # Never drop pending messages on restart.
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await backend.client.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
