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

# ========== КОНФИГУРАЦИЯ (Рекомендуется использовать переменные окружения) ==========
TG_TOKEN = "8757845092:AAHK3sKNbzy-w7_4oBgHF7p9sYeNGxIXEic"
GEMINI_API_KEY = "AIzaSyDJAiv1aUdtqWv8qbMyvuXhfztCJTSQBtU"
DEEPSEEK_API_KEY = "sk-4c6ad799713d41d8b22f614be5e02264"
TAVILY_API_KEY = "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"

DB_PATH = "simul_core.db"
MAX_HISTORY_CHARS = 3000  # Ограничение истории для экономии токенов

# ========== ИНИЦИАЛИЗАЦИЯ КЛИЕНТОВ ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
deepseek_client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com/v1")

try:
    from tavily import AsyncTavilyClient
    tavily_client = AsyncTavilyClient(api_key=TAVILY_API_KEY)
    TAVILY_ENABLED = True
except ImportError:
    TAVILY_ENABLED = False
    logger.error("Tavily library not found! pip install tavily-python")

# ========== СОСТОЯНИЯ И БД ==========
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
            return {"bot_name": row[0], "history": row[1] or ""} if row else None

async def update_user(user_id, bot_name=None, history=None):
    async with aiosqlite.connect(DB_PATH) as db:
        if bot_name is not None:
            await db.execute("""
                INSERT INTO users (user_id, bot_name, history) VALUES (?, ?, '')
                ON CONFLICT(user_id) DO UPDATE SET bot_name = excluded.bot_name
            """, (user_id, bot_name))
        if history is not None:
            # Обрезаем историю, если она слишком длинная
            if len(history) > MAX_HISTORY_CHARS:
                history = "..." + history[-MAX_HISTORY_CHARS:]
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

# ========== ЛОГИКА ПОИСКА ==========
async def fetch_search_results(query: str) -> str:
    if not TAVILY_ENABLED:
        return ""
    try:
        # Улучшенный поиск через Tavily
        search = await tavily_client.search(query=query, search_depth="advanced", max_results=4)
        results = search.get('results', [])
        if not results:
            return ""
        
        context = "\nДАННЫЕ ИЗ ИНТЕРНЕТА (АКТУАЛЬНО):\n"
        for i, r in enumerate(results, 1):
            context += f"{i}. {r.get('title')}: {r.get('content')}\n"
        return context
    except Exception as e:
        logger.error(f"Search error: {e}")
        return ""

# ========== ОТПРАВКА СООБЩЕНИЙ ==========
async def safe_send(message: aiog_types.Message, text: str, keyboard=None):
    if not text or text.strip() == "":
        text = "⚠️ Извините, я не смог сформировать ответ. Попробуйте еще раз."
    
    # Telegram лимит 4096 символов
    limit = 4000
    for i in range(0, len(text), limit):
        chunk = text[i:i+limit]
        await message.answer(chunk, reply_markup=keyboard if i + limit >= len(text) else None)

# ========== ОСНОВНОЙ ОБРАБОТЧИК ИИ ==========
async def get_ai_response(prompt, user_data, search_data=""):
    bot_name = user_data['bot_name']
    history = user_data['history']
    
    persona = f"Ты — {bot_name}, продвинутый ИИ-ассистент системы Simul BM-100. "
    if search_data:
        persona += "Используй предоставленные данные из интернета для максимально точного ответа. "
    
    full_prompt = f"{persona}\n\nИстория диалога:\n{history}\n\n{search_data}\nПользователь: {prompt}\nОтвет {bot_name}:"

    # Попытка 1: Gemini
    try:
        response = await gemini_client.aio.models.generate_content(
            model="gemini-1.5-flash",
            contents=full_prompt
        )
        if response.text:
            return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini failed: {e}")

    # Попытка 2: DeepSeek (резерв)
    try:
        resp = await deepseek_client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": full_prompt}],
            max_tokens=1024
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"DeepSeek failed: {e}")
        return None

# ========== ОБРАБОТЧИКИ AIOGRAM ==========
bot = Bot(token=TG_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
active_search_users = set()

@dp.message(Command("start"))
async def cmd_start(message: aiog_types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data:
        await message.answer(f"🦾 Simul BM-100 активен.\nАссистент {user_data['bot_name']} в сети.", reply_markup=get_main_keyboard())
    else:
        await message.answer("🦾 Протокол инициализации Simul.\n\nВведите имя, которое будет носить ваш ассистент:")
        await state.set_state(Registration.waiting_for_bot_name)

@dp.message(Registration.waiting_for_bot_name)
async def register_name(message: aiog_types.Message, state: FSMContext):
    if not message.text:
        return await message.answer("Имя должно быть текстовым!")
    name = message.text.strip()[:30]
    await update_user(message.from_user.id, bot_name=name)
    await state.clear()
    await message.answer(f"✅ Протокол завершен. Я — {name}.", reply_markup=get_main_keyboard())

@dp.message(F.text == "🔍 Начать поиск")
async def start_search(message: aiog_types.Message):
    active_search_users.add(message.from_user.id)
    await message.answer("🔍 Режим расширенного поиска активирован. Каждый запрос будет проверяться в сети.")

@dp.message(F.text == "⏹ Закончить поиск")
async def end_search(message: aiog_types.Message):
    active_search_users.discard(message.from_user.id)
    await message.answer("⏹ Режим поиска отключен.")

@dp.message(F.text == "🗑 Очистить память")
async def clear_mem(message: aiog_types.Message):
    await update_user(message.from_user.id, history="")
    await message.answer("🧠 Память очищена.")

@dp.message(F.text == "ℹ️ Информация")
async def info_btn(message: aiog_types.Message):
    data = await get_user_data(message.from_user.id)
    status = "Активен" if message.from_user.id in active_search_users else "Выключен"
    await message.answer(f"🤖 Имя: {data['bot_name']}\n🌍 Поиск: {status}\n📊 Память: {len(data['history'])} симв.")

@dp.message()
async def main_handler(message: aiog_types.Message):
    user_id = message.from_user.id
    user_data = await get_user_data(user_id)
    
    if not user_data:
        return await message.answer("Пожалуйста, введите /start")

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        search_context = ""
        user_query = message.text or message.caption or ""

        # Проверка на необходимость поиска (включен режим или есть вопросы в тексте)
        is_searching = user_id in active_search_users
        keywords = ['кто', 'что', 'где', 'когда', 'курс', 'новости', 'погода', 'сколько', 'почему']
        
        if (is_searching or any(kw in user_query.lower() for kw in keywords)) and user_query:
            search_context = await fetch_search_results(user_query)

        # Если прислано фото
        if message.photo:
            try:
                photo = message.photo[-1]
                # Используем новый метод aiogram 3.x
                file_info = await bot.get_file(photo.file_id)
                photo_bytes = await bot.download_file(file_info.file_path)
                
                img = Image.open(photo_bytes)
                if img.mode != 'RGB': img = img.convert('RGB')
                
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='JPEG')
                
                img_part = types.Part.from_bytes(data=img_byte_arr.getvalue(), mime_type="image/jpeg")
                prompt = user_query if user_query else "Что на этом фото?"
                
                response = await gemini_client.aio.models.generate_content(
                    model="gemini-1.5-flash",
                    contents=[f"Ассистент {user_data['bot_name']}. {prompt}", img_part]
                )
                ai_reply = response.text
            except Exception as e:
                logger.error(f"Photo processing error: {e}")
                ai_reply = "❌ Ошибка при анализе фото."
        else:
            # Текстовый запрос
            ai_reply = await get_ai_response(user_query, user_data, search_context)

        if ai_reply:
            # Сохраняем в историю (без огромного поискового контекста, только суть)
            new_history = f"{user_data['history']}\nПользователь: {user_query}\nАссистент: {ai_reply}"
            await update_user(user_id, history=new_history)
            await safe_send(message, ai_reply, get_main_keyboard())
        else:
            await message.answer("⚠️ Системная ошибка ИИ. Попробуйте позже.")

async def main():
    await init_db()
    logger.info("--- БОТ ЗАПУЩЕН ---")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass