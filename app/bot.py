import asyncio
import html
import logging
from io import BytesIO
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo, FSInputFile

from app.config import Settings
from app.qr import MAX_IMAGE_BYTES, make_pass_image, read_qr
from app.security import InvalidToken

router = Router()


class Registration(StatesGroup):
    name = State()


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
    text = f"<b>{html.escape(card['name'])}</b>\n{html.escape(card['event_name'])}\nКатегория: {html.escape(card['category'])}\nБаллы: <b>{card['points']}</b>\nПриход: {status}"
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
async def start(message: Message, backend: Backend, settings: Settings, state: FSMContext):
    if message.chat.type != "private":
        return
    await state.clear()
    if message.text and message.text.split(maxsplit=1)[-1] == "review":
        await review(message, backend, settings)
        return
    try:
        await onboarding(message, settings)
        registration = await backend.request("GET", f"/internal/registration/{message.from_user.id}")
        if registration["registered"]:
            # Re-registering the same saved name only renews an expired pass.
            await backend.request("POST", "/internal/register", json={"telegram_id": message.from_user.id, "name": registration["name"]})
            await send_qr(message, backend, settings)
        else:
            await state.set_state(Registration.name)
            await message.answer("Как вас записать на мероприятие? Напишите фамилию и имя одним сообщением. Например: Петров Иван.")
    except ApiError as exc:
        await message.answer(str(exc))


async def onboarding(message: Message, settings: Settings):
    text = (
        "Добро пожаловать в GuestPass! Здесь можно получить пропуск и проверить регистрацию гостей.\n\n"
        "Организатор может проверить гостя двумя способами:\n"
        "• Сфотографировать QR обычной камерой и отправить фото прямо в этот чат. Можно и переслать картинку: бот сам распознает её.\n"
        "• Открыть Mini App кнопкой GuestPass в меню бота: там есть камера, загрузка фото и ввод кода.\n\n"
        "Нет возможности отправить фото? Вставьте текстовый код GP1.… из сообщения с пропуском.\n\n"
        "Проверяете задание? Нажмите кнопку ниже или отправьте /review. Откроете общее демомероприятие и получите два тестовых QR. Имена участников видны другим проверяющим. Можно открыть QR на компьютере и сфотографировать его телефоном.\n\n"
        "Скан открывает карточку. Баллы добавляются только кнопкой «Отметить приход», один раз."
    )
    rows = [[InlineKeyboardButton(text="Попробовать роль организатора", callback_data="onboarding:review")]]
    if settings.public_url.startswith("https://"):
        rows.append([InlineKeyboardButton(text="Открыть Mini App", web_app=WebAppInfo(url=settings.public_url + "/"))])
    await message.answer_photo(
        FSInputFile(Path(__file__).parent / "static/assets/onboarding-methods.png"),
        caption=text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "onboarding:review")
async def onboarding_review(query: CallbackQuery, backend: Backend, settings: Settings, state: FSMContext | None = None):
    if not query.message or query.message.chat.type != "private":
        await query.answer("Откройте личный чат с ботом", show_alert=True)
        return
    await query.answer()
    await review(query.message, backend, settings, actor_id=query.from_user.id, state=state)


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
        png = await asyncio.to_thread(
            make_pass_image,
            data["token"],
            (await backend.request("GET", f"/internal/registration/{message.from_user.id}"))["name"] or message.from_user.full_name,
            settings.event_name,
        )
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
        await message.answer(
            "Этот раздел доступен организаторам. Для проверки задания отправьте /review — получите роль организатора демомероприятия."
        )
        return
    await organizer_guide(message, settings)


async def organizer_guide(message: Message, settings: Settings):
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
async def review(message: Message, backend: Backend, settings: Settings, actor_id: int | None = None, state: FSMContext | None = None):
    if message.chat.type != "private":
        return
    if state:
        await state.clear()
    try:
        result = await backend.request("POST", "/internal/review", json={"actor_id": actor_id or message.from_user.id})
        await message.answer_photo(
            FSInputFile(Path(__file__).parent / "static/assets/onboarding-review.png"),
            caption="Общее демомероприятие: откройте QR на компьютере и сфотографируйте телефоном. Фото можно отправить в этот чат или распознать в Mini App. Код GP1.… тоже работает.",
        )
        await message.answer(
            "Вы организатор общего демомероприятия. Здесь видны все зарегистрированные через /start участники. Ниже — два QR вымышленных гостей для первого теста. Перешлите любое из этих фото обратно боту или скопируйте код: появится карточка и кнопка начисления. Повторите действие, чтобы проверить защиту от повторных баллов.\n\n/organizer — открыть веб-сканер\n/event — выйти из тестового режима\n\nСчётчики общие для всех аккаунтов. Каждый новый участник увеличивает число регистраций. Повторное сканирование одним или разными организаторами не добавляет баллы второй раз."
        )
        for guest in result["guests"]:
            png = await asyncio.to_thread(make_pass_image, guest["token"], guest["name"], "Общее демомероприятие")
            await message.answer_photo(
                BufferedInputFile(png, filename="review-qr.png"),
                caption=f"Тестовый гость: {guest['name']}\nПерешлите это фото обратно в этот чат.",
            )
            await message.answer(f"<code>{html.escape(guest['token'])}</code>", parse_mode="HTML")
        await organizer_guide(message, settings)
    except ApiError as exc:
        await message.answer(str(exc))


@router.message(Command("event"))
async def leave_review(message: Message, backend: Backend):
    if message.chat.type == "private":
        try:
            await backend.request("POST", "/internal/leave-review", json={"actor_id": message.from_user.id})
            await message.answer(
                "Роль проверяющего выключена. /qr — ваш пропуск на основную встречу; /review — получить роль организатора."
            )
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


@router.message(Command("name"))
async def change_name(message: Message, state: FSMContext):
    if message.chat.type == "private":
        await state.set_state(Registration.name)
        await message.answer("Напишите фамилию и имя одним сообщением. Например: Петров Иван.")


def valid_name(text: str) -> bool:
    words = text.split()
    return (
        2 <= len(words) <= 5
        and 3 <= len(text) <= 100
        and all(char.isalpha() or char in " -'’" for char in text)
        and all(any(c.isalpha() for c in word) for word in words)
    )


@router.message(Registration.name, F.text, ~F.text.startswith("/"))
async def save_name(message: Message, backend: Backend, settings: Settings, state: FSMContext):
    if message.chat.type != "private":
        return
    name = " ".join(message.text.split())
    if not valid_name(name):
        await message.answer("Нужны фамилия и имя буквами, например: Петров Иван. Код QR вводится после регистрации или через /review.")
        return
    try:
        await backend.request("POST", "/internal/register", json={"telegram_id": message.from_user.id, "name": name})
        await state.clear()
        await message.answer(f"Готово, {name}! Ниже ваш персональный пропуск. /name — исправить имя.")
        await send_qr(message, backend, settings)
    except ApiError as exc:
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
