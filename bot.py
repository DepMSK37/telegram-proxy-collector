import asyncio
import json
import os
import time
from pathlib import Path
from os import environ

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiogram.enums import ParseMode
from aiogram.client.session.aiohttp import AiohttpSession

BOT_TOKEN = environ["BOT_TOKEN"]
BOT_PROXY = environ["BOT_PROXY"]

VERIFIED_DIR    = Path("verified")
RATE_LIMIT_FILE = Path("rate_limit.json")

CACHE_TTL     = 1 * 3600
USER_COOLDOWN = 1 * 3600
MAX_MSG_LEN   = 4096

PROXY_FILES = {
    "eu":  VERIFIED_DIR / "proxy_eu_verified.txt",
    "ru":  VERIFIED_DIR / "proxy_ru_verified.txt",
    "all": VERIFIED_DIR / "proxy_all_verified.txt",
}

REGION_LABELS = {
    "eu":  "🌍 EU",
    "ru":  "🇷🇺 RU",
    "all": "🌐 Все регионы",
}

session = AiohttpSession(proxy=BOT_PROXY)
bot = Bot(token=BOT_TOKEN, session=session)
dp  = Dispatcher()

_collector_running = False


def _load_limits() -> dict:
    if RATE_LIMIT_FILE.exists():
        try:
            return json.loads(RATE_LIMIT_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_limits(data: dict) -> None:
    RATE_LIMIT_FILE.write_text(json.dumps(data), encoding="utf-8")


def check_cooldown(user_id: int) -> int | None:
    limits = _load_limits()
    uid = str(user_id)
    if uid in limits:
        elapsed = time.time() - limits[uid]
        if elapsed < USER_COOLDOWN:
            return int(USER_COOLDOWN - elapsed)
    return None


def set_cooldown(user_id: int) -> None:
    limits = _load_limits()
    limits[str(user_id)] = time.time()
    _save_limits(limits)


def cache_age_seconds(region: str) -> float | None:
    path = PROXY_FILES.get(region)
    if path and path.exists():
        return time.time() - path.stat().st_mtime
    return None


def read_proxy_lines(region: str) -> list[str]:
    path = PROXY_FILES.get(region)
    if not path or not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def split_by_length(lines: list[str], max_len: int = MAX_MSG_LEN) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        needed = len(line) + (1 if current else 0)
        if current and current_len + needed > max_len:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += needed
    if current:
        chunks.append("\n".join(current))
    return chunks


async def run_collector() -> bool:
    global _collector_running
    if _collector_running:
        return False
    _collector_running = True
    try:
        proc = await asyncio.create_subprocess_exec(
            "python", "main.py",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.communicate()
        return proc.returncode == 0
    except Exception:
        return False
    finally:
        _collector_running = False


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🌍 EU прокси",  callback_data="proxy_eu"),
            InlineKeyboardButton(text="🇷🇺 RU прокси", callback_data="proxy_ru"),
        ],
        [
            InlineKeyboardButton(text="🌐 Все прокси", callback_data="proxy_all"),
        ],
    ])


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "👋 <b>MTProto Proxy Bot</b>\n\n"
        "Получай свежие рабочие MTProto-прокси для Telegram.\n"
        "Каждая ссылка добавляет прокси в один клик прямо из чата.\n\n"
        "⏱ <i>Лимит: один запрос раз в 4 часа</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


@dp.callback_query(F.data.in_({"proxy_eu", "proxy_ru", "proxy_all"}))
async def handle_proxy_request(call: CallbackQuery) -> None:
    region  = call.data.removeprefix("proxy_")
    user_id = call.from_user.id
    label   = REGION_LABELS[region]

    remaining = check_cooldown(user_id)
    if remaining is not None:
        h = remaining // 3600
        m = (remaining % 3600) // 60
        await call.answer(
            f"⏳ Следующий запрос доступен через {h} ч {m} мин",
            show_alert=True,
        )
        return

    await call.answer()
    set_cooldown(user_id)

    age     = cache_age_seconds(region)
    proxies = read_proxy_lines(region)
    stale   = (age is not None) and (age > CACHE_TTL)

    if not proxies:
        wait = await call.message.answer(
            "⏳ <b>База пустая</b>, собираю прокси (~2–3 мин)...\nПожалуйста, подожди.",
            parse_mode=ParseMode.HTML,
        )
        await run_collector()
        proxies = read_proxy_lines(region)
        await wait.delete()

        if not proxies:
            await call.message.answer(
                "😔 Не удалось найти рабочие прокси. Попробуй чуть позже.",
                reply_markup=main_keyboard(),
            )
            return

        await _send_proxies(call.message, proxies, label, stale=False)
        return

    await _send_proxies(call.message, proxies, label, stale=stale)

    if stale and not _collector_running:
        asyncio.create_task(run_collector())


async def _send_proxies(
    message: Message,
    proxies: list[str],
    label: str,
    stale: bool,
) -> None:
    count  = len(proxies)
    chunks = split_by_length(proxies)

    stale_note = (
        "\n⚠️ <i>Данные устарели (&gt;4ч) — фоново обновляю базу</i>"
        if stale else ""
    )
    await message.answer(
        f"{label} прокси — <b>{count} шт.</b>{stale_note}\n"
        f"Нажми на ссылку → прокси добавится в Telegram автоматически 👇",
        parse_mode=ParseMode.HTML,
    )

    for chunk in chunks:
        await message.answer(chunk)

    await message.answer(
        "✅ Готово! Если прокси не работает — попробуй следующий.",
        reply_markup=main_keyboard(),
    )


async def main() -> None:
    VERIFIED_DIR.mkdir(exist_ok=True)
    print("🤖 Бот запущен (polling)")
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
