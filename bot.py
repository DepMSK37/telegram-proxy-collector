import asyncio
import os
import sys
import time
import json
import subprocess
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, LinkPreviewOptions

# Токен бота берется из файла .env (переменные окружения)
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "укажи_токен_в_env")

if BOT_TOKEN == "укажи_токен_в_env":
    print("ВНИМАНИЕ: Токен бота не найден! Проверьте файл .env")

# Настройки кэширования и лимитов
CACHE_TIME_SECONDS = 4 * 3600  # 4 часа (свежесть базы прокси)
USER_COOLDOWN_SECONDS = 4 * 3600  # 4 часа (кулдаун для пользователя)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Блокировка для предотвращения параллельного запуска main.py
parsing_lock = asyncio.Lock()

# Файл для сохранения времени последних запросов от пользователей
COOLDOWN_FILE = "user_cooldowns.json"

def load_cooldowns() -> dict:
    if os.path.exists(COOLDOWN_FILE):
        try:
            with open(COOLDOWN_FILE, "r", encoding="utf-8") as f:
                return {int(k): v for k, v in json.load(f).items()}
        except Exception:
            return {}
    return {}

def save_cooldowns(data: dict):
    with open(COOLDOWN_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

# Клавиатура выбора региона
def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇪🇺 Получить прокси", callback_data="get_proxy_eu")]
    ])

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я — комбайн свежих и быстрых MTProto прокси.\n\n"
        "Выбери, какие прокси тебе нужны, нажав на кнопку ниже. "
        "База обновляется каждые 4 часа.",
        reply_markup=get_main_keyboard()
    )

async def check_user_cooldown(user_id: int) -> tuple[bool, str]:
    """Проверяет, прошло ли 6 часов с момента последнего запроса."""
    cooldowns = load_cooldowns()
    last_request_time = cooldowns.get(user_id, 0)
    current_time = time.time()
    
    if current_time - last_request_time < USER_COOLDOWN_SECONDS:
        remaining = USER_COOLDOWN_SECONDS - (current_time - last_request_time)
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        return False, f"⏳ Вы уже получали прокси недавно. Следующий запрос будет доступен через {hours} ч. {minutes} мин."
    return True, ""

async def update_user_cooldown(user_id: int):
    """Обновляет время запроса пользователя."""
    cooldowns = load_cooldowns()
    cooldowns[user_id] = time.time()
    save_cooldowns(cooldowns)

async def send_long_text(message: types.Message, file_path: str):
    """Читает файл и отправляет текст частями, чтобы не превысить лимит Telegram."""
    if not os.path.exists(file_path):
        await message.answer("❌ Ошибка: файл с прокси не найден.")
        return
        
    with open(file_path, 'r', encoding='utf-8') as f:
        text = f.read()
        
    if not text.strip():
        await message.answer("📭 Файл пуст, похоже прокси не нашлись. Попробуй позже.")
        return

    # Разбиваем текст по строкам, чтобы не разорвать ссылку посередине
    lines = text.split('\n')
    chunk = ""
    
    for line in lines:
        if len(chunk) + len(line) + 1 > 4000:
            await message.answer(chunk, link_preview_options=LinkPreviewOptions(is_disabled=True))
            chunk = line + '\n'
            await asyncio.sleep(0.3)  # Небольшая пауза для защиты от спам-фильтров Telegram API
        else:
            chunk += line + '\n'
            
    if chunk.strip():
        await message.answer(chunk, link_preview_options=LinkPreviewOptions(is_disabled=True))

async def update_proxies_if_needed(message: types.Message) -> bool:
    """Запускает парсер, если прокси старше заданного кэша. Возвращает True в случае успеха."""
    file_path = "verified/proxy_all_verified.txt"
    need_parse = True
    
    if os.path.exists(file_path):
        mtime = os.path.getmtime(file_path)
        if time.time() - mtime < CACHE_TIME_SECONDS:
            need_parse = False
            
    if need_parse:
        if parsing_lock.locked():
            await message.answer("⏳ База прокси прямо сейчас обновляется. Пожалуйста, подождите...")
            async with parsing_lock:
                return True 
        else:
            async with parsing_lock:
                status_msg = await message.answer("⚙️ Свежих прокси нет в кэше. Запускаю сбор и проверку. Это займет 1-2 минуты...")
                try:
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, "main.py",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    await process.communicate()
                    await status_msg.delete()
                    return True
                except Exception as e:
                    await status_msg.edit_text(f"❌ Произошла ошибка при сборе прокси: {e}")
                    return False
    return True

@dp.callback_query(F.data.startswith("get_proxy_"))
async def process_get_proxy(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    region = callback.data.split("_")[-1] # eu, ru, all
    
    allowed, msg = await check_user_cooldown(user_id)
    if not allowed:
        await callback.answer(msg, show_alert=True)
        return
        
    await callback.answer() # Убираем "часики" спиннера с кнопки
    
    if isinstance(callback.message, types.Message):
        success = await update_proxies_if_needed(callback.message)
        if not success:
            return
    else:
        return
        
    file_map = {
        "eu": "verified/proxy_eu_verified.txt",
        "ru": "verified/proxy_ru_verified.txt",
        "all": "verified/proxy_all_verified.txt"
    }
    target_file = file_map.get(region)
    if not target_file:
        await callback.message.answer("❌ Неизвестный регион.")
        return
    
    if isinstance(callback.message, types.Message):
        await callback.message.answer("🚀 Держи твой список прокси! Просто нажимай на ссылки:")
        await send_long_text(callback.message, target_file)
    
    await update_user_cooldown(user_id)

async def main():
    print("🤖 Бот запущен! Ожидаю сообщений...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
