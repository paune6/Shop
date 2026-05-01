import asyncio
import logging
import aiosqlite
import io
import os
from datetime import datetime
from PIL import Image

# Библиотеки ИИ и Поиска
from google import genai
from google.genai import types
from tavily import AsyncTavilyClient
from duckduckgo_search import DDGS

# Aiogram 3.x
from aiogram import Bot, Dispatcher, types as aiog_types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.chat_action import ChatActionSender

# ========== НАСТРОЙКИ СИСТЕМЫ ==========
API_CONFIG = {
    "TG": "8757845092:AAHK3sKNbzy-w7_4oBgHF7p9sYeNGxIXEic",
    "GEMINI": "AIzaSyDJAiv1aUdtqWv8qbMyvuXhfztCJTSQBtU",
    "TAVILY": "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"
}

DB_PATH = "simul_core.db"
MODEL_ID = "gemini-1.5-flash" # Самая быстрая и стабильная для поиска

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SimulOmni")

# ========== КЛИЕНТЫ ==========
gemini = genai.Client(api_key=API_CONFIG["GEMINI"])
tavily = AsyncTavilyClient(api_key=API_CONFIG["TAVILY"])

# ========== СИСТЕМА ГЛОБАЛЬНОГО ПОИСКА ==========
async def simul_search_engine(query: str) -> str:
    """Собирает знания со всей сети"""
    combined_data = ""
    
    # 1. Попытка через Tavily (Фактология)
    try:
        search = await asyncio.wait_for(
            tavily.search(query=query, search_depth="advanced", max_results=4),
            timeout=10.0
        )
        for r in search.get("results", []):
            combined_data += f"Источник: {r['title']}\nСуть: {r['content']}\n\n"
    except Exception as e:
        logger.warning(f"Tavily search skipped: {e}")

    # 2. Попытка через DuckDuckGo (Новости/Браузер)
    if not combined_data.strip():
        try:
            def sync_ddg():
                with DDGS() as ddgs:
                    return [r for r in ddgs.text(query, max_results=3)]
            ddg_res = await asyncio.to_thread(sync_ddg)
            for r in ddg_res:
                combined_data += f"Источник: {r['title']}\nСуть: {r['body']}\n\n"
        except Exception as e:
            logger.error(f"DDG search skipped: {e}")

    return combined_data.strip()

# ========== БАЗА ДАННЫХ ==========
async def db_manager():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, 
                name TEXT, 
                history TEXT, 
                search_mode INTEGER DEFAULT 1
            )""")
        await db.commit()

async def get_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT name, history, search_mode FROM users WHERE id=?", (uid,)) as c:
            row = await c.fetchone()
            if row: return {"name": row[0], "history": row[1] or "", "search": bool(row[2])}
            return None

# ========== ИНТЕРФЕЙС ==========
def get_keyboard(s_status: bool):
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="ℹ️ Статус"), KeyboardButton(text="🗑 Очистить память")],
            [KeyboardButton(text="🔍 Поиск: ВКЛ" if not s_status else "⏹ Поиск: ВЫКЛ")],
            [KeyboardButton(text="⚙️ Сменить имя")]
        ], resize_keyboard=True
    )

bot = Bot(token=API_CONFIG["TG"])
dp = Dispatcher(storage=MemoryStorage())

class Reg(StatesGroup):
    name = State()

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def cmd_start(message: aiog_types.Message, state: FSMContext):
    await state.clear()
    user = await get_user(message.from_user.id)
    if user:
        await message.answer(f"🦾 Simul активен. Я готов ответить на любой вопрос.", reply_markup=get_keyboard(user['search']))
    else:
        await message.answer("🦾 Протокол инициализации.\nКак мне называть тебя?")
        await state.set_state(Reg.name)

@dp.message(Reg.name)
async def process_name(message: aiog_types.Message, state: FSMContext):
    name = message.text.strip()[:20] if message.text else "Командор"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users (id, name, history, search_mode) VALUES (?, ?, '', 1) ON CONFLICT(id) DO UPDATE SET name=?", (message.from_user.id, name, name))
        await db.commit()
    await state.clear()
    await message.answer(f"✅ Принято, {name}. Я подключен к глобальной сети.", reply_markup=get_keyboard(True))

@dp.message(F.text.contains("Поиск:"))
async def toggle_search(message: aiog_types.Message):
    user = await get_user(message.from_user.id)
    new_status = 0 if user['search'] else 1
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET search_mode=? WHERE id=?", (new_status, message.from_user.id))
        await db.commit()
    await message.answer(f"✅ Режим поиска {'АКТИВИРОВАН' if new_status else 'ДЕАКТИВИРОВАН'}.", reply_markup=get_keyboard(bool(new_status)))

@dp.message(F.text == "🗑 Очистить память")
async def clear_mem(message: aiog_types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET history='' WHERE id=?", (message.from_user.id,))
        await db.commit()
    await message.answer("🧠 Память очищена.")

@dp.message()
async def omni_handler(message: aiog_types.Message, state: FSMContext):
    uid = message.from_user.id
    user = await get_user(uid)
    if not user: return

    # Проверка кнопок меню
    if message.text in ["ℹ️ Статус", "⚙️ Сменить имя"]:
        if message.text == "ℹ️ Статус":
            await message.answer(f"🤖 Simul BM-100\n👤 Твое имя: {user['name']}\n🌍 Поиск: {'ВКЛ' if user['search'] else 'ВЫКЛ'}")
        return

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        query = message.text or message.caption or ""
        web_knowledge = ""
        
        # Решаем, нужно ли лезть в интернет
        # Ищем если: включен режим ИЛИ есть ключевые слова вопроса
        triggers = ["кто", "что", "когда", "где", "почему", "курс", "новости", "сколько", "цена", "события"]
        if (user['search'] or any(t in query.lower() for t in triggers)) and query:
            web_knowledge = await simul_search_engine(query)

        # Системная установка (делает его "всезнающим")
        sys_instr = (
            f"Ты — Simul, универсальный ИИ-помощник пользователя {user['name']}. "
            f"Текущая дата: {datetime.now().strftime('%d.%m.%Y')}. "
            "ИНСТРУКЦИЯ: Ты имеешь прямой доступ к интернету. Данные из интернета будут предоставлены ниже. "
            "Ты ОБЯЗАН использовать их для ответа, если вопрос касается фактов, цифр или событий. "
            "Если данных нет, используй свою внутреннюю базу знаний на 100%."
        )

        final_prompt = f"{sys_instr}\n\n[ЗНАНИЯ ИЗ СЕТИ]:\n{web_knowledge}\n\n[КОНТЕКСТ ДИАЛОГА]:\n{user['history']}\n\n[ВОПРОС]: {query}"

        try:
            # Если прислано фото
            if message.photo:
                photo = message.photo[-1]
                p_file = await bot.get_file(photo.file_id)
                p_data = await bot.download_file(p_file.file_path)
                img = Image.open(p_data).convert("RGB")
                img.thumbnail((1200, 1200))
                img_io = io.BytesIO()
                img.save(img_io, format="JPEG", quality=85)
                
                resp = await gemini.aio.models.generate_content(
                    model=MODEL_ID,
                    contents=[f"{sys_instr}\n{query}", types.Part.from_bytes(data=img_io.getvalue(), mime_type="image/jpeg")]
                )
                answer = resp.text
            # Если текст
            else:
                resp = await gemini.aio.models.generate_content(
                    model=MODEL_ID,
                    contents=final_prompt
                )
                answer = resp.text
        except Exception as e:
            logger.error(f"Core Error: {e}")
            answer = "⚠️ Системный сбой ИИ. Попробуйте переформулировать вопрос или очистить память."

        if answer:
            # Обновление истории
            new_hist = (user['history'] + f"\nU: {query}\nA: {answer}")[-3500:]
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("UPDATE users SET history=? WHERE id=?", (new_hist, uid))
                await db.commit()
            
            # Если был поиск — добавляем маркер
            if web_knowledge:
                answer = "🌐 [SIMUL KNOWLEDGE ACTIVE]\n\n" + answer

            for i in range(0, len(answer), 4000):
                await message.answer(answer[i:i+4000], reply_markup=get_keyboard(user['search']))

async def main():
    await db_manager()
    # Сброс старых обновлений для Bothost
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("--- SIMUL OMNI-ENGINE STARTED ---")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())