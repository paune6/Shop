import asyncio
import logging
import traceback
import aiosqlite
import io
from PIL import Image
from google import genai
from google.genai.types import Part

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.utils.chat_action import ChatActionSender

# Для повторных попыток при ошибках сети
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# --- КОНФИГУРАЦИЯ ---
TG_TOKEN = "8757845092:AAHK3sKNbzy-w7_4oBgHF7p9sYeNGxIXEic"
GEMINI_API_KEY = "AIzaSyDJAiv1aUdtqWv8qbMyvuXhfztCJTSQBtU"
DB_PATH = "simul_core.db"
MAX_IMAGE_SIZE_MB = 20  # Лимит для обработки фото

logging.basicConfig(level=logging.INFO)

client = genai.Client(api_key=GEMINI_API_KEY)

# --- СОСТОЯНИЯ ---
class Registration(StatesGroup):
    waiting_for_bot_name = State()

class SearchMode(StatesGroup):
    active = State()

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                bot_name TEXT,
                history TEXT
            )
        """)
        await db.commit()

async def get_user_data(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT bot_name, history FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        if row:
            return {"bot_name": row[0], "history": row[1] or ""}
        return None

async def update_user(user_id, bot_name=None, history=None):
    async with aiosqlite.connect(DB_PATH) as db:
        # Гарантируем существование записи
        await db.execute("INSERT OR IGNORE INTO users (user_id, bot_name, history) VALUES (?, '', '')", (user_id,))
        if bot_name is not None:
            await db.execute("UPDATE users SET bot_name = ? WHERE user_id = ?", (bot_name, user_id))
        if history is not None:
            await db.execute("UPDATE users SET history = ? WHERE user_id = ?", (history, user_id))
        await db.commit()

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="ℹ️ Информация"), KeyboardButton(text="🗑 Очистить память")],
            [KeyboardButton(text="⚙️ Изменить имя"), KeyboardButton(text="❓ Помощь")],
            [KeyboardButton(text="Начать поиск"), KeyboardButton(text="Закончить поиск")],
            [KeyboardButton(text="📊 Статус системы")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

# --- ИНИЦИАЛИЗАЦИЯ ---
bot = Bot(token=TG_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ВЫЗОВА GEMINI С РЕТРАЕМ ---
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True
)
async def generate_with_retry(model, contents):
    # Выполняем синхронный вызов в потоке
    return await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=contents
    )

# --- ОБРАБОТЧИКИ КОМАНД ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data:
        await message.answer(
            f"Simul - BM 100\nВаш персональный ассистент {user_data['bot_name']} готов к выполнению задач.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            "🦾 Протокол Инициализации Simul\n\nОтправьте имя, под которым вы хотите общаться с системой:"
        )
        await state.set_state(Registration.waiting_for_bot_name)

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "🤖 Справка Simul - BM 100\n\n"
        "/start – запуск\n/help – команды\n\n"
        "Кнопки:\n"
        "• Информация – статус\n"
        "• Очистить память – сброс истории\n"
        "• Изменить имя – задать новое имя\n"
        "• Помощь – эта справка\n"
        "• Начать/Закончить поиск – режим поиска\n"
        "• Статус системы – состояние ядра"
    )
    await message.answer(help_text)

# --- РЕГИСТРАЦИЯ ИМЕНИ ---
@dp.message(Registration.waiting_for_bot_name)
async def process_reg(message: types.Message, state: FSMContext):
    if message.text:
        name = message.text.strip()[:20]
        await update_user(message.from_user.id, bot_name=name)
        await state.clear()
        await message.answer(
            f"✅ Синхронизация завершена.\n\nSimul - BM 100\nЯ — {name}.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer("Пожалуйста, отправьте текст для имени.")

# --- КНОПКИ МЕНЮ ---
@dp.message(F.text == "ℹ️ Информация")
async def btn_info(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data:
        await message.answer(
            f"🟢 Simul онлайн\nМодель: BM 100\nИмя: {user_data['bot_name']}\n"
            f"Память: {len(user_data['history'])} симв.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer("Система не инициализирована. Используйте /start.", reply_markup=get_main_keyboard())

@dp.message(F.text == "🗑 Очистить память")
async def btn_reset(message: types.Message):
    await update_user(message.from_user.id, history="")
    await message.answer("🧠 Память очищена.", reply_markup=get_main_keyboard())

@dp.message(F.text == "⚙️ Изменить имя")
async def btn_change_name(message: types.Message, state: FSMContext):
    await message.answer("Введите новое имя:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(Registration.waiting_for_bot_name)

@dp.message(F.text == "❓ Помощь")
async def btn_help(message: types.Message):
    await cmd_help(message)

@dp.message(F.text == "📊 Статус системы")
async def btn_sys_status(message: types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data:
        await message.answer(
            f"🟢 Simul онлайн\nМодель: BM 100\nИмя: {user_data['bot_name']}\n"
            f"Память: {len(user_data['history'])} симв.",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer("Система не инициализирована. /start", reply_markup=get_main_keyboard())

# --- РЕЖИМ ПОИСКА ---
search_users = set()

@dp.message(F.text == "Начать поиск")
async def btn_start_search(message: types.Message, state: FSMContext):
    search_users.add(message.from_user.id)
    await message.answer("🔍 Режим поиска активирован. Все ваши запросы теперь будут обрабатываться как поисковые.",
                         reply_markup=get_main_keyboard())
    # Отдельный стейт, чтобы отличать от обычного режима (не обязательно, используем множество)

@dp.message(F.text == "Закончить поиск")
async def btn_end_search(message: types.Message):
    search_users.discard(message.from_user.id)
    await message.answer("⏹ Режим поиска остановлен. Вы снова в обычном диалоге.",
                         reply_markup=get_main_keyboard())

# --- ГЛАВНЫЙ ОБРАБОТЧИК СООБЩЕНИЙ ---
@dp.message()
async def universal_handler(message: types.Message, state: FSMContext):
    # Игнорируем сообщения, которые не являются текстом или фото
    if not message.photo and not message.text:
        return

    user_id = message.from_user.id
    user_data = await get_user_data(user_id)

    # Автоматический старт, если данные утеряны
    if not user_data:
        await message.answer(
            "⚠️ Сессия утеряна. Давайте восстановим.\nОтправьте имя ассистента заново:",
            reply_markup=types.ReplyKeyboardRemove()
        )
        await state.set_state(Registration.waiting_for_bot_name)
        return

    bot_name = user_data["bot_name"]
    history = user_data["history"]

    # Определяем режим поиска
    in_search = user_id in search_users

    # Подготовка содержимого для Gemini
    prompt_parts = [
        f"Ты — Simul, ИИ-ассистент с именем {bot_name}. "
        f"{'Сейчас тебя попросили выполнить поиск по запросу.' if in_search else 'Продолжи диалог.'}"
        f"История диалога:\n{history}"
    ]

    user_text = None
    try:
        if message.photo:
            # Обработка фото
            photo = message.photo[-1]
            file_info = await bot.get_file(photo.file_id)
            if file_info.file_size and file_info.file_size > MAX_IMAGE_SIZE_MB * 1024 * 1024:
                await message.answer(f"❌ Изображение слишком большое. Максимум {MAX_IMAGE_SIZE_MB} МБ.")
                return

            file_io = io.BytesIO()
            await bot.download_file(file_info.file_path, file_io)
            file_io.seek(0)
            try:
                img = Image.open(file_io)
            except Exception as e:
                logging.error(f"Невозможно открыть изображение: {e}")
                await message.answer("❌ Не удалось обработать изображение. Попробуйте другой формат.")
                return

            user_text = message.caption if message.caption else "Что на этом фото?"
            prompt_parts.append(Part.from_image(img))  # Правильная часть изображения
            prompt_parts.append(f"Пользователь прислал фото и спросил: {user_text}")
        else:
            user_text = message.text
            prompt_parts.append(f"Пользователь: {user_text}")

        # Генерация ответа с авто-повтором
        response = await generate_with_retry(model="gemini-2.0-flash-exp", contents=prompt_parts)
        ai_reply = response.text.strip()

        # Обновление истории (если не фото и не поиск)
        if not message.photo and not in_search and user_text:
            new_history = (history + f"\nU: {user_text}\nA: {ai_reply}")[-4000:]
            await update_user(user_id, history=new_history)
        elif not in_search:
            # Фото не заносим в историю, но зато можно добавить краткую информацию
            logging.info("Фото-запрос обработан без сохранения истории.")

        await message.answer(ai_reply, reply_markup=get_main_keyboard())

    except Exception as e:
        logging.error(f"Ошибка при генерации ответа: {traceback.format_exc()}")
        await message.answer(
            "⚠️ Произошла ошибка при обращении к ядру Simul.\n"
            "Попробуйте ещё раз или введите другой запрос.",
            reply_markup=get_main_keyboard()
        )

# --- ЗАПУСК ---
async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())