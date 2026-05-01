import asyncio
import logging
import aiosqlite
import io
from PIL import Image
from google import genai # Новый импорт

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.chat_action import ChatActionSender

# --- КОНФИГУРАЦИЯ ---
TG_TOKEN = "8757845092:AAHK3sKNbzy-w7_4oBgHF7p9sYeNGxIXEic"
GEMINI_API_KEY = "AIzaSyDJAiv1aUdtqWv8qbMyvuXhfztCJTSQBtU"
DB_PATH = "simul_core.db"

logging.basicConfig(level=logging.INFO)

# Инициализация нового клиента Gemini
client = genai.Client(api_key=GEMINI_API_KEY)

# --- СОСТОЯНИЯ ---
class Registration(StatesGroup):
    waiting_for_bot_name = State()

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
        async with db.execute("SELECT bot_name, history FROM users WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone()

async def update_user(user_id, bot_name=None, history=None):
    async with aiosqlite.connect(DB_PATH) as db:
        if bot_name is not None:
            await db.execute("""
                INSERT OR REPLACE INTO users (user_id, bot_name, history) 
                VALUES (?, ?, COALESCE((SELECT history FROM users WHERE user_id = ?), ''))
            """, (user_id, bot_name, user_id))
        if history is not None:
            await db.execute("UPDATE users SET history = ? WHERE user_id = ?", (history, user_id))
        await db.commit()

# --- КЛАВИАТУРА ---
def get_main_menu():
    buttons = [
        [InlineKeyboardButton(text="🗑 Очистить память", callback_data="reset_history")],
        [InlineKeyboardButton(text="⚙️ Изменить имя", callback_data="change_name")],
        [InlineKeyboardButton(text="📊 Статус системы", callback_data="sys_status")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ИНИЦИАЛИЗАЦИЯ ---
bot = Bot(token=TG_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- ОБРАБОТЧИКИ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data:
        await message.answer(
            f"🌟 **Ядро Simul онлайн.**\nВаш персональный ассистент **{user_data[0]}** готов к выполнению задач.",
            reply_markup=get_main_menu(), parse_mode="Markdown"
        )
    else:
        await message.answer("🦾 **Протокол Инициализации Simul**\n\nЯ — самая мощная ИИ-система в Telegram. Чтобы начать работу, дайте мне уникальное имя:")
        await state.set_state(Registration.waiting_for_bot_name)

@dp.callback_query(F.data == "reset_history")
async def handle_reset(callback: types.CallbackQuery):
    await update_user(callback.from_user.id, history="")
    await callback.answer("🧠 Память очищена!", show_alert=True)
    await callback.message.answer("Контекст диалога был сброшен до заводских настроек.")

@dp.callback_query(F.data == "change_name")
async def handle_change_name(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите новое имя для системы Simul:")
    await state.set_state(Registration.waiting_for_bot_name)
    await callback.answer()

@dp.callback_query(F.data == "sys_status")
async def handle_sys_status(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_data = await get_user_data(user_id)
    
    if user_data:
        bot_name, history = user_data
        history_len = len(history) if history else 0
        await callback.answer(
            f"🟢 Система Simul онлайн\nИмя ассистента: {bot_name}\nИспользование памяти: {history_len} символов", 
            show_alert=True
        )
    else:
        await callback.answer("❌ Система не инициализирована. Используйте /start.", show_alert=True)

@dp.message(Registration.waiting_for_bot_name)
async def process_reg(message: types.Message, state: FSMContext):
    if message.text:
        name = message.text.strip()[:20]
        await update_user(message.from_user.id, bot_name=name)
        await state.clear()
        await message.answer(
            f"✅ Синхронизация завершена. Приятно познакомиться, я — **{name}**.", 
            reply_markup=get_main_menu(), 
            parse_mode="Markdown"
        )
    else:
        await message.answer("Пожалуйста, отправьте текст для имени системы.")

# --- ГЛАВНАЯ ЛОГИКА (ТЕКСТ + ФОТО) ---

@dp.message()
async def universal_handler(message: types.Message):
    user_id = message.from_user.id
    user_data = await get_user_data(user_id)

    if not user_data:
        await message.answer("Пожалуйста, используйте /start для активации системы.")
        return

    bot_name, history = user_data
    history = history or ""  # предотвращает ошибку, если history равно None

    # Эффект "Печатает..."
    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        prompt_parts = [
            f"Ты — Simul, самая совершенная ИИ-модель. Твоё имя: {bot_name}. Твои ответы всегда точны, глубоки и полезны. "
            f"Контекст беседы: {history}"
        ]

        # Если пользователь прислал фото
        if message.photo:
            photo = message.photo[-1]
            file_info = await bot.get_file(photo.file_id)
            
            # Скачивание файла в буфер
            file_io = io.BytesIO()
            await bot.download_file(file_info.file_path, file_io)
            file_io.seek(0)
            img = Image.open(file_io)
            
            prompt_parts.append(img)
            
            user_text = message.caption if message.caption else "Что на этом фото?"
            prompt_parts.append(f"\nПользователь прислал фото и спросил: {user_text}")
        elif message.text:
            prompt_parts.append(f"\nПользователь: {message.text}")

        try:
            # Генерация ответа через новый метод клиента
            response = await asyncio.to_thread(
                client.models.generate_content,
                model='gemini-1.5-flash',
                contents=prompt_parts
            )
            ai_reply = response.text

            # Обновление истории (только для текста)
            if not message.photo and message.text:
                new_history = (history + f"\nU: {message.text}\nA: {ai_reply}")[-3000:]
                await update_user(user_id, history=new_history)

            await message.answer(ai_reply, parse_mode="Markdown", reply_markup=get_main_menu())
        except Exception as e:
            logging.error(f"Error: {e}")
            await message.answer("⚠️ Произошел системный сбой в ядре Simul. Попробуйте другой запрос.")

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())