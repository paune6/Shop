import asyncio
import logging
import aiosqlite
import io
from datetime import datetime
from PIL import Image

# AI SDK
from google import genai
from google.genai import types
from duckduckgo_search import DDGS # Поиск без ключей

# Aiogram
from aiogram import Bot, Dispatcher, types as aiog_types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.chat_action import ChatActionSender

# ========== КОНФИГУРАЦИЯ ==========
API_KEYS = {
    "TG": "8757845092:AAHK3sKNbzy-w7_4oBgHF7p9sYeNGxIXEic",
    "GEMINI": "AIzaSyDJAiv1aUdtqWv8qbMyvuXhfztCJTSQBtU",
    "TAVILY": "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"
}

DB_PATH = "simul_core.db"
# Используем стабильную версию модели
GEMINI_MODEL = "gemini-1.5-flash" 

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SimulBot")

# ========== КЛИЕНТЫ ==========
gemini_client = genai.Client(api_key=API_KEYS["GEMINI"])

# ========== СИСТЕМА ПОИСКА "БРАУЗЕР" (БЕЗЛИМИТНАЯ) ==========
async def simul_browser_search(query: str):
    """Пытается найти ответ в сети через DuckDuckGo (бесплатно)"""
    try:
        logger.info(f"🌐 Браузер ищет: {query}")
        with DDGS() as ddgs:
            # Берем первые 5 результатов
            results = [r for r in ddgs.text(query, max_results=5)]
        
        if not results:
            return ""

        context = "\n--- [ДАННЫЕ ИЗ ОТКРЫТЫХ ИСТОЧНИКОВ] ---\n"
        for r in results:
            context += f"📌 {r['title']}\n{r['body']}\nИсточник: {r['href']}\n\n"
        return context
    except Exception as e:
        logger.error(f"DDG Search error: {e}")
        return ""

# ========== БАЗА ДАННЫХ ==========
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, name TEXT, history TEXT)")
        await db.commit()

async def get_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name, history FROM users WHERE id=?", (uid,)) as c:
            row = await c.fetchone()
            return {"name": row[0], "history": row[1] or ""} if row else None

# ========== КЛАВИАТУРА ==========
def get_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="ℹ️ Инфо"), KeyboardButton(text="🗑 Сброс памяти")],
            [KeyboardButton(text="🔍 Поиск: ВКЛ"), KeyboardButton(text="⏹ Поиск: ВЫКЛ")]
        ], resize_keyboard=True
    )

bot = Bot(token=API_KEYS["TG"])
dp = Dispatcher(storage=MemoryStorage())
active_search = set()

class Reg(StatesGroup):
    name = State()

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def start(message: aiog_types.Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    if user:
        await message.answer(f"🦾 Simul в сети. Я — {user['name']}", reply_markup=get_kb())
    else:
        await message.answer("🦾 Инициализация Simul.\nКак мне тебя называть?")
        await state.set_state(Reg.name)

@dp.message(Reg.name)
async def set_name(message: aiog_types.Message, state: FSMContext):
    name = message.text[:20] if message.text else "Пользователь"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users (id, name, history) VALUES (?, ?, '') ON CONFLICT(id) DO UPDATE SET name=?", 
                         (message.from_user.id, name, name))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Принято. Теперь я твой Simul-ассистент.", reply_markup=get_kb())

@dp.message(F.text == "🔍 Поиск: ВКЛ")
async def s_on(message: aiog_types.Message):
    active_search.add(message.from_user.id)
    await message.answer("🔍 Режим браузера включен. Буду искать всё в интернете.")

@dp.message(F.text == "⏹ Поиск: ВЫКЛ")
async def s_off(message: aiog_types.Message):
    active_search.discard(message.from_user.id)
    await message.answer("⏹ Поиск выключен.")

@dp.message(F.text == "🗑 Сброс памяти")
async def clear_m(message: aiog_types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET history='' WHERE id=?", (message.from_user.id,))
        await db.commit()
    await message.answer("🧠 Память очищена.")

@dp.message()
async def main_handler(message: aiog_types.Message):
    uid = message.from_user.id
    user = await get_user(uid)
    if not user: return

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        user_input = message.text or message.caption or ""
        search_context = ""
        
        # Авто-поиск если включен или вопрос
        if (uid in active_search or any(t in user_input.lower() for t in ["кто", "что", "где", "курс", "новости"])) and user_input:
            search_context = await simul_browser_search(user_input)

        # Промпт-инструкция (Делает его умным)
        sys_instr = (
            f"Ты — Simul BM-100, ИИ-ассистент с доступом в интернет. "
            f"Сейчас {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
            "Если предоставлены данные из открытых источников, используй их для точного ответа. "
            "Отвечай на языке пользователя."
        )

        full_prompt = f"{sys_instr}\n\nИстория:\n{user['history']}\n{search_context}\nЗапрос: {user_input}\nОтвет:"

        try:
            # Обработка фото
            if message.photo:
                photo = message.photo[-1]
                p_file = await bot.get_file(photo.file_id)
                p_data = await bot.download_file(p_file.file_path)
                img = Image.open(p_data).convert("RGB")
                img_io = io.BytesIO()
                img.save(img_io, format="JPEG")
                
                res = await gemini_client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=[f"{sys_instr}\n{user_input}", types.Part.from_bytes(data=img_io.getvalue(), mime_type="image/jpeg")]
                )
                ai_reply = res.text
            # Обработка текста
            else:
                res = await asyncio.wait_for(
                    gemini_client.aio.models.generate_content(model=GEMINI_MODEL, contents=full_prompt),
                    timeout=20.0
                )
                ai_reply = res.text
        except Exception as e:
            logger.error(f"Gemini error: {e}")
            ai_reply = "⚠️ Ошибка: Модель временно недоступна. Попробуй позже."

        if ai_reply:
            # Сохранение в историю (последние 3000 символов)
            new_hist = (user['history'] + f"\nU: {user_input}\nA: {ai_reply}")[-3000:]
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET history=? WHERE id=?", (new_hist, uid))
                await db.commit()
            
            # Отправка ответа
            for i in range(0, len(ai_reply), 4000):
                await message.answer(ai_reply[i:i+4000], reply_markup=get_kb())

async def main():
    await init_db()
    # Удаляем старые запросы (ConflictError fix)
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("--- БОТ ЗАПУЩЕН (BROWSER MODE) ---")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())