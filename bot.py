import asyncio
import logging
import aiosqlite
import io
import os
from datetime import datetime
from PIL import Image

# Модели и Поиск
from google import genai
from google.genai import types
from tavily import AsyncTavilyClient
from duckduckgo_search import DDGS

# Aiogram
from aiogram import Bot, Dispatcher, types as aiog_types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.chat_action import ChatActionSender

# ========== КОНФИГУРАЦИЯ ==========
KEYS = {
    "TG": "8757845092:AAHK3sKNbzy-w7_4oBgHF7p9sYeNGxIXEic",
    "GEMINI": "AIzaSyDJAiv1aUdtqWv8qbMyvuXhfztCJTSQBtU",
    "TAVILY": "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"
}

DB_PATH = "simul_core.db"
GEMINI_MODEL = "gemini-1.5-flash"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SimulSearch")

# ========== КЛИЕНТЫ ==========
gemini_client = genai.Client(api_key=KEYS["GEMINI"])
tavily_client = AsyncTavilyClient(api_key=KEYS["TAVILY"])

# ========== УЛЬТИМАТИВНЫЙ ПОИСК ==========
async def get_simul_knowledge(query: str):
    """Функция гарантированного получения данных из сети"""
    search_data = ""
    
    # 1. Сначала пробуем Tavily (самый надежный API)
    try:
        logger.info(f"🔎 Tavily ищет: {query}")
        t_res = await asyncio.wait_for(
            tavily_client.search(query=query, search_depth="basic", max_results=4),
            timeout=8.0
        )
        for r in t_res.get("results", []):
            search_data += f"Источник: {r['title']}\nКонтент: {r['content']}\n\n"
    except Exception as e:
        logger.warning(f"Tavily failed: {e}")

    # 2. Если Tavily пуст, пробуем DuckDuckGo (через поток, чтобы не вис хостинг)
    if not search_data.strip():
        try:
            logger.info(f"🔎 DDG ищет: {query}")
            def ddg_sync():
                with DDGS() as ddgs:
                    return [r for r in ddgs.text(query, max_results=3)]
            
            ddg_res = await asyncio.to_thread(ddg_sync)
            for r in ddg_res:
                search_data += f"Источник: {r['title']}\nКонтент: {r['body']}\n\n"
        except Exception as e:
            logger.error(f"DDG failed: {e}")

    return search_data.strip()

# ========== БАЗА ДАННЫХ ==========
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, 
                name TEXT, 
                history TEXT, 
                search_on INTEGER DEFAULT 0
            )""")
        await db.commit()

async def db_get_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name, history, search_on FROM users WHERE id=?", (uid,)) as c:
            row = await c.fetchone()
            if row: return {"name": row[0], "history": row[1] or "", "search": bool(row[2])}
            return None

# ========== КЛАВИАТУРА ==========
def main_kb(s_status: bool):
    s_btn = "⏹ Поиск: ВЫКЛ" if s_status else "🔍 Поиск: ВКЛ"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="ℹ️ Инфо"), KeyboardButton(text="🗑 Очистка")],
            [KeyboardButton(text=s_btn)],
            [KeyboardButton(text="⚙️ Сменить имя")]
        ], resize_keyboard=True
    )

bot = Bot(token=KEYS["TG"])
dp = Dispatcher(storage=MemoryStorage())

class Reg(StatesGroup):
    name = State()

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def cmd_start(message: aiog_types.Message, state: FSMContext):
    await state.clear()
    user = await db_get_user(message.from_user.id)
    if user:
        await message.answer(f"🦾 Simul онлайн. Привет, {user['name']}.", reply_markup=main_kb(user['search']))
    else:
        await message.answer("🦾 Инициализация Simul BM-100.\nВведите ваше имя:")
        await state.set_state(Reg.name)

@dp.message(Reg.name)
async def process_name(message: aiog_types.Message, state: FSMContext):
    name = message.text.strip()[:20] if message.text else "Пользователь"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users (id, name, history, search_on) VALUES (?, ?, '', 0) ON CONFLICT(id) DO UPDATE SET name=?", (message.from_user.id, name, name))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Имя {name} принято.", reply_markup=main_kb(False))

@dp.message(F.text.contains("Поиск:"))
async def toggle_search(message: aiog_types.Message):
    user = await db_get_user(message.from_user.id)
    new_s = 0 if user['search'] else 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET search_on=? WHERE id=?", (new_s, message.from_user.id))
        await db.commit()
    await message.answer(f"✅ Поиск {'ВКЛЮЧЕН' if new_s else 'ВЫКЛЮЧЕН'}.", reply_markup=main_kb(bool(new_s)))

@dp.message(F.text == "🗑 Очистка")
async def clear_mem(message: aiog_types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET history='' WHERE id=?", (message.from_user.id,))
        await db.commit()
    await message.answer("🧠 Память сброшена.")

@dp.message()
async def main_handler(message: aiog_types.Message, state: FSMContext):
    uid = message.from_user.id
    user = await db_get_user(uid)
    if not user: return

    # Игнор служебных кнопок
    if message.text in ["ℹ️ Инфо", "⚙️ Сменить имя"]:
        if message.text == "ℹ️ Инфо":
            await message.answer(f"👤 Имя: {user['name']}\n🌍 Режим поиска: {'ВКЛ' if user['search'] else 'ВЫКЛ'}")
        return

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        query = message.text or message.caption or ""
        knowledge = ""
        
        # 1. ПРОВЕРКА НЕОБХОДИМОСТИ ПОИСКА
        triggers = ["кто", "что", "где", "курс", "новости", "цена", "сколько", "когда"]
        need_search = user['search'] or any(t in query.lower() for t in triggers)
        
        if need_search and query:
            knowledge = await get_simul_knowledge(query)

        # 2. ФОРМИРОВАНИЕ УЛЬТИМАТИВНОГО ПРОМПТА
        sys_instr = (
            f"Ты — Simul, ИИ-ассистент пользователя {user['name']}. "
            f"Сегодняшняя дата: {datetime.now().strftime('%d.%m.%Y')}. "
            "ИНСТРУКЦИЯ: Ниже приведены данные из интернета. Ты ОБЯЗАН использовать их для ответа на вопрос пользователя. "
            "Если данных нет в блоке поиска, используй свои знания."
        )

        full_prompt = f"{sys_instr}\n\n[БЛОК ПОИСКА]:\n{knowledge}\n\n[ИСТОРИЯ]:\n{user['history']}\n\n[ВОПРОС]: {query}"

        try:
            # 3. ОБРАБОТКА ФОТО
            if message.photo:
                photo = message.photo[-1]
                p_file = await bot.get_file(photo.file_id)
                p_data = await bot.download_file(p_file.file_path)
                img = Image.open(p_data).convert("RGB")
                img.thumbnail((1024, 1024))
                img_io = io.BytesIO()
                img.save(img_io, format="JPEG", quality=85)
                
                resp = await gemini_client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=[f"{sys_instr}\n{query}", types.Part.from_bytes(data=img_io.getvalue(), mime_type="image/jpeg")]
                )
                reply = resp.text
            # 4. ТЕКСТОВЫЙ ЗАПРОС
            else:
                resp = await gemini_client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=full_prompt
                )
                reply = resp.text
        except Exception as e:
            logger.error(f"Gemini error: {e}")
            reply = "⚠️ Модель временно недоступна. Попробуйте еще раз."

        if reply:
            # Сохранение в историю (лимит 3000 симв)
            new_h = (user['history'] + f"\nU: {query}\nA: {reply}")[-3000:]
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET history=? WHERE id=?", (new_h, uid))
                await db.commit()
            
            # Если был поиск, добавим визуальный индикатор
            if knowledge:
                reply = "🌐 [Simul Search Active]\n\n" + reply

            for i in range(0, len(reply), 4000):
                await message.answer(reply[i:i+4000], reply_markup=main_kb(user['search']))

async def main():
    await db_init()
    # Жесткий сброс для предотвращения ConflictError
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("--- SIMUL BM-100 УСПЕШНО ЗАПУЩЕН ---")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())