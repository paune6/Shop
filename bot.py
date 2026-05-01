import asyncio
import logging
import aiosqlite
import io
from datetime import datetime
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
CONFIG = {
    "TG_TOKEN": "8757845092:AAHK3sKNbzy-w7_4oBgHF7p9sYeNGxIXEic",
    "GEMINI_KEY": "AIzaSyDJAiv1aUdtqWv8qbMyvuXhfztCJTSQBtU",
    "DEEPSEEK_KEY": "sk-4c6ad799713d41d8b22f614be5e02264",
    "TAVILY_KEY": "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",
    "DB_PATH": "simul_core.db",
    "MAX_HIST": 3500
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SimulCore")

# ========== ИНИЦИАЛИЗАЦИЯ КЛИЕНТОВ ==========
gemini = genai.Client(api_key=CONFIG["GEMINI_KEY"])
deepseek = AsyncOpenAI(api_key=CONFIG["DEEPSEEK_KEY"], base_url="https://api.deepseek.com/v1")

try:
    from tavily import AsyncTavilyClient
    tavily = AsyncTavilyClient(api_key=CONFIG["TAVILY_KEY"])
except ImportError:
    tavily = None

# ========== СИСТЕМА ПОИСКА И ПАМЯТИ ==========
class SimulCore:
    @staticmethod
    async def get_search(query: str):
        if not tavily: return ""
        try:
            search = await tavily.search(query=query, search_depth="advanced", max_results=4)
            res = search.get("results", [])
            if not res: return ""
            
            context = f"\n[АКТУАЛЬНЫЕ ДАННЫЕ ИЗ СЕТИ НА {datetime.now().strftime('%d.%m.%Y')}]:\n"
            for item in res:
                context += f"• {item.get('title')}: {item.get('content')}\n"
            return context
        except Exception as e:
            logger.error(f"Search error: {e}")
            return ""

    @staticmethod
    async def init_db():
        async with aiosqlite.connect(CONFIG["DB_PATH"]) as db:
            await db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, history TEXT)")
            await db.commit()

# ========== СОСТОЯНИЯ ==========
class Reg(StatesGroup):
    waiting_name = State()

def get_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="ℹ️ Инфо"), KeyboardButton(text="🗑 Очистить память")],
            [KeyboardButton(text="🔍 Режим поиска: ВКЛ"), KeyboardButton(text="⏹ Поиск: ВЫКЛ")],
            [KeyboardButton(text="⚙️ Сменить имя")]
        ], resize_keyboard=True
    )

# ========== ГЛАВНАЯ ЛОГИКА БОТА ==========
bot = Bot(token=CONFIG["TG_TOKEN"])
dp = Dispatcher(storage=MemoryStorage())
search_mode_users = set()

async def get_ai_answer(prompt, user_data, search_data=""):
    bot_name = user_data[0]
    history = user_data[1]
    
    sys_prompt = (
        f"Ты — {bot_name}, мощный ИИ ассистент системы Simul BM-100. "
        "Твоя задача: давать максимально точные, проверенные и полезные ответы. "
        "Если тебе предоставлены 'АКТУАЛЬНЫЕ ДАННЫЕ ИЗ СЕТИ', всегда используй их как главный источник правды."
    )
    
    full_query = f"{sys_prompt}\n\nИстория диалога:\n{history}\n\n{search_data}\nПользователь: {prompt}\n{bot_name}:"

    # 1. Пробуем Gemini
    try:
        response = await gemini.aio.models.generate_content(
            model="gemini-1.5-flash",
            contents=full_query
        )
        if response.text: return response.text.strip()
    except Exception as e:
        logger.warning(f"Gemini fallback to DeepSeek: {e}")

    # 2. Резерв — DeepSeek
    try:
        ds_res = await deepseek.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": full_query}],
            max_tokens=1500
        )
        return ds_res.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"DeepSeek error: {e}")
        return "❌ Ошибка системы. Попробуйте позже."

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def start(message: aiog_types.Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(CONFIG["DB_PATH"]) as db:
        async with db.execute("SELECT name FROM users WHERE id=?", (message.from_user.id,)) as c:
            user = await c.fetchone()
            if user:
                await message.answer(f"🦾 Simul активен. Я — {user[0]}.", reply_markup=get_kb())
            else:
                await message.answer("🦾 Протокол инициализации Simul.\nВведите имя ассистента:")
                await state.set_state(Reg.waiting_name)

@dp.message(Reg.waiting_name)
async def set_name(message: aiog_types.Message, state: FSMContext):
    name = message.text.strip()[:25] if message.text else "Ассистент"
    async with aiosqlite.connect(CONFIG["DB_PATH"]) as db:
        await db.execute("INSERT INTO users (id, name, history) VALUES (?, ?, '') ON CONFLICT(id) DO UPDATE SET name=?", (message.from_user.id, name, name))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Готово. Теперь я {name}.", reply_markup=get_kb())

@dp.message(F.text == "🗑 Очистить память")
async def clear_hist(message: aiog_types.Message):
    async with aiosqlite.connect(CONFIG["DB_PATH"]) as db:
        await db.execute("UPDATE users SET history='' WHERE id=?", (message.from_user.id,))
        await db.commit()
    await message.answer("🧠 Память сброшена.")

@dp.message(F.text.startswith("🔍 Режим поиска"))
async def search_on(message: aiog_types.Message):
    search_mode_users.add(message.from_user.id)
    await message.answer("🔍 Поиск в интернете теперь работает для каждого сообщения.")

@dp.message(F.text == "⏹ Поиск: ВЫКЛ")
async def search_off(message: aiog_types.Message):
    search_mode_users.discard(message.from_user.id)
    await message.answer("⏹ Поиск по умолчанию отключен.")

@dp.message(F.text == "⚙️ Сменить имя")
async def change_name(message: aiog_types.Message, state: FSMContext):
    await message.answer("Введите новое имя:")
    await state.set_state(Reg.waiting_name)

@dp.message()
async def handle_all(message: aiog_types.Message):
    user_id = message.from_user.id
    
    async with aiosqlite.connect(CONFIG["DB_PATH"]) as db:
        async with db.execute("SELECT name, history FROM users WHERE id=?", (user_id,)) as c:
            user_data = await c.fetchone()
    
    if not user_data: return

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        user_msg = message.text or message.caption or ""
        search_context = ""
        
        # Интеллектуальное решение о поиске
        smart_triggers = ["кто", "что", "почему", "новости", "курс", "когда", "где", "погода"]
        if user_id in search_mode_users or any(x in user_msg.lower() for x in smart_triggers):
            if user_msg:
                search_context = await SimulCore.get_search(user_msg)

        # Если фото
        if message.photo:
            try:
                photo = message.photo[-1]
                p_file = await bot.get_file(photo.file_id)
                p_bytes = await bot.download_file(p_file.file_path)
                
                # Конвертация в RGB для Gemini
                img = Image.open(p_bytes).convert("RGB")
                img_io = io.BytesIO()
                img.save(img_io, format="JPEG")
                
                img_part = types.Part.from_bytes(data=img_io.getvalue(), mime_type="image/jpeg")
                
                resp = await gemini.aio.models.generate_content(
                    model="gemini-1.5-flash",
                    contents=[f"Ассистент {user_data[0]}. Ответь на запрос: {user_msg}", img_part]
                )
                answer = resp.text
            except Exception as e:
                logger.error(f"Image error: {e}")
                answer = "❌ Не удалось распознать изображение."
        else:
            # Текст
            answer = await get_ai_answer(user_msg, user_data, search_context)

        # Сохранение истории
        if answer:
            new_history = f"{user_data[1]}\nU: {user_msg}\nA: {answer}"
            if len(new_history) > CONFIG["MAX_HIST"]:
                new_history = "..." + new_history[-CONFIG["MAX_HIST"]:]
            
            async with aiosqlite.connect(CONFIG["DB_PATH"]) as db:
                await db.execute("UPDATE users SET history=? WHERE id=?", (new_history, user_id))
                await db.commit()
            
            # Отправка длинных сообщений
            limit = 4000
            for i in range(0, len(answer), limit):
                await message.answer(answer[i:i+limit], reply_markup=get_kb())

async def main():
    await SimulCore.init_db()
    logger.info("SIMUL BM-100 STARTED")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())