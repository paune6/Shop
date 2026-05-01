import asyncio
import logging
import aiosqlite
import io
from PIL import Image

# Библиотеки ИИ
from google import genai
from google.genai import types
from openai import AsyncOpenAI

# Aiogram
from aiogram import Bot, Dispatcher, types as aiog_types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.chat_action import ChatActionSender

# ========== КОНФИГУРАЦИЯ ==========
TG_TOKEN = "8757845092:AAHK3sKNbzy-w7_4oBgHF7p9sYeNGxIXEic"
GEMINI_API_KEY = "AIzaSyDJAiv1aUdtqWv8qbMyvuXhfztCJTSQBtU"
DEEPSEEK_API_KEY = "sk-4c6ad799713d41d8b22f614be5e02264"
TAVILY_API_KEY = "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"

DB_PATH = "simul_core.db"
MAX_HISTORY_CHARS = 3000 

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ========== КЛИЕНТЫ ==========
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
deepseek_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1")

try:
    from tavily import AsyncTavilyClient
    tavily_client = AsyncTavilyClient(api_key=TAVILY_API_KEY)
    TAVILY_ENABLED = True
except ImportError:
    TAVILY_ENABLED = False

# ========== БД ==========
class Registration(StatesGroup):
    waiting_for_bot_name = State()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, 
                bot_name TEXT, 
                history TEXT
            )""")
        await db.commit()

async def get_user_data(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT bot_name, history FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"bot_name": row[0], "history": row[1] or ""}
            return None

async def update_user(user_id, bot_name=None, history=None):
    async with aiosqlite.connect(DB_PATH) as db:
        if bot_name is not None:
            await db.execute("""
                INSERT INTO users (user_id, bot_name, history) VALUES (?, ?, '')
                ON CONFLICT(user_id) DO UPDATE SET bot_name = excluded.bot_name
            """, (user_id, bot_name))
        if history is not None:
            if len(history) > MAX_HISTORY_CHARS:
                history = history[-MAX_HISTORY_CHARS:]
            await db.execute("UPDATE users SET history = ? WHERE user_id = ?", (history, user_id))
        await db.commit()

# ========== КЛАВИАТУРА ==========
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="ℹ️ Информация"), KeyboardButton(text="🗑 Очистить память")],
            [KeyboardButton(text="⚙️ Изменить имя"), KeyboardButton(text="❓ Помощь")],
            [KeyboardButton(text="🔍 Начать поиск"), KeyboardButton(text="⏹ Закончить поиск")],
            [KeyboardButton(text="📊 Статус системы")]
        ],
        resize_keyboard=True
    )

# ========== ПОИСК ==========
async def fetch_search_results(query: str) -> str:
    if not TAVILY_ENABLED or not query:
        return ""
    try:
        search = await tavily_client.search(query=query, search_depth="advanced", max_results=3)
        results = search.get('results', [])
        if not results: return ""
        
        context = "\nИНФОРМАЦИЯ ИЗ СЕТИ:\n"
        for r in results:
            context += f"- {r.get('title')}: {r.get('content')}\n"
        return context
    except Exception as e:
        logger.error(f"Search error: {e}")
        return ""

# ========== ОТПРАВКА ==========
async def safe_send(message: aiog_types.Message, text: str, keyboard=None):
    if not text:
        text = "⚠️ Ошибка: ИИ прислал пустой ответ."
    
    limit = 4000
    for i in range(0, len(text), limit):
        chunk = text[i:i+limit]
        await message.answer(chunk, reply_markup=keyboard if i + limit >= len(text) else None)

# ========== ГЛАВНЫЙ ОБРАБОТЧИК (ИСПРАВЛЕН) ==========
bot = Bot(token=TG_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
active_search_users = set()

@dp.message(Command("start"))
async def cmd_start(message: aiog_types.Message, state: FSMContext):
    await state.clear() # Сбрасываем всё при старте
    user_data = await get_user_data(message.from_user.id)
    if user_data:
        await message.answer(f"🦾 Simul BM-100 готов. Я — {user_data['bot_name']}", reply_markup=get_main_keyboard())
    else:
        await message.answer("🤖 Протокол инициализации.\nВведите имя вашего ассистента:")
        await state.set_state(Registration.waiting_for_bot_name)

@dp.message(Registration.waiting_for_bot_name)
async def register_name(message: aiog_types.Message, state: FSMContext):
    if not message.text:
        return await message.answer("Введите текстовое имя.")
    name = message.text.strip()[:30]
    await update_user(message.from_user.id, bot_name=name)
    await state.clear()
    await message.answer(f"✅ Имя {name} сохранено. Задавайте любые вопросы!", reply_markup=get_main_keyboard())

@dp.message(F.text == "🔍 Начать поиск")
async def start_search(message: aiog_types.Message):
    active_search_users.add(message.from_user.id)
    await message.answer("🔍 Поиск включен для всех сообщений.")

@dp.message(F.text == "⏹ Закончить поиск")
async def end_search(message: aiog_types.Message):
    active_search_users.discard(message.from_user.id)
    await message.answer("⏹ Поиск отключен.")

@dp.message(F.text == "🗑 Очистить память")
async def clear_mem(message: aiog_types.Message):
    await update_user(message.from_user.id, history="")
    await message.answer("🧠 Память очищена.")

@dp.message(F.text == "⚙️ Изменить имя")
async def change_name(message: aiog_types.Message, state: FSMContext):
    await message.answer("Введите новое имя ассистента:")
    await state.set_state(Registration.waiting_for_bot_name)

# Обработка всех сообщений (Вопросы, Фото, Текст)
@dp.message()
async def universal_handler(message: aiog_types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = await get_user_data(user_id)
    
    # Если пользователя нет в базе - просим регистрацию
    if not user_data:
        current_state = await state.get_state()
        if current_state != Registration.waiting_for_bot_name:
            await message.answer("Пожалуйста, сначала введите имя ассистента.")
            await state.set_state(Registration.waiting_for_bot_name)
        return

    # Если это просто текст меню, игнорируем (они обработаны выше)
    if message.text in ["ℹ️ Информация", "📊 Статус системы", "❓ Помощь"]:
        if message.text == "❓ Помощь":
            await message.answer("Я — ИИ ассистент. Просто пиши вопрос или присылай фото!")
        return

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        user_query = message.text or message.caption or ""
        search_data = ""

        # 1. Поиск (если включен или если в тексте вопрос)
        keywords = ['кто', 'что', 'когда', 'курс', 'новости', 'почему', 'найди', 'как']
        if (user_id in active_search_users or any(k in user_query.lower() for k in keywords)) and user_query:
            search_data = await fetch_search_results(user_query)

        # 2. Формируем промпт
        persona = f"Ты — {user_data['bot_name']}. Отвечай кратко и понятно."
        history = user_data['history']
        full_context = f"{persona}\n\nИстория:\n{history}\n\n{search_data}\nПользователь: {user_query}\nОтвет:"

        ai_reply = None

        # 3. Обработка Фото через Gemini
        if message.photo:
            try:
                photo = message.photo[-1]
                file_io = await bot.download(photo)
                img = Image.open(file_io)
                if img.mode != 'RGB': img = img.convert('RGB')
                
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='JPEG')
                
                img_part = types.Part.from_bytes(data=img_byte_arr.getvalue(), mime_type="image/jpeg")
                
                resp = await gemini_client.aio.models.generate_content(
                    model="gemini-1.5-flash",
                    contents=[f"Ассистент {user_data['bot_name']}. {user_query}", img_part]
                )
                ai_reply = resp.text
            except Exception as e:
                logger.error(f"Photo error: {e}")
                ai_reply = "❌ Не удалось проанализировать фото."

        # 4. Обработка Текстового вопроса
        else:
            try:
                # Сначала Gemini
                resp = await gemini_client.aio.models.generate_content(
                    model="gemini-1.5-flash",
                    contents=full_context
                )
                ai_reply = resp.text
            except Exception as e:
                logger.error(f"Gemini error: {e}")
                # Резерв - DeepSeek
                try:
                    ds_resp = await deepseek_client.chat.completions.create(
                        model="deepseek-chat",
                        messages=[{"role": "user", "content": full_context}]
                    )
                    ai_reply = ds_resp.choices[0].message.content
                except Exception as e2:
                    logger.error(f"DeepSeek error: {e2}")

        # 5. Сохранение и ответ
        if ai_reply:
            new_history = f"{history}\nU: {user_query}\nA: {ai_reply}"
            await update_user(user_id, history=new_history)
            await safe_send(message, ai_reply, get_main_keyboard())
        else:
            await message.answer("⚠️ Не удалось получить ответ. Попробуйте переформулировать вопрос.")

async def main():
    await init_db()
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())