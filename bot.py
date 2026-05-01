import asyncio
import logging
import aiosqlite
import io
import concurrent.futures
from PIL import Image
from google import genai
from google.genai import types
from openai import AsyncOpenAI
import httpx

from aiogram import Bot, Dispatcher, types as aiog_types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.chat_action import ChatActionSender

# ========== Tavily ==========
try:
    from tavily import TavilyClient
    TAVILY_AVAILABLE = True
except ImportError:
    TAVILY_AVAILABLE = False
    logging.error("tavily-python not installed. Run: pip install tavily-python")

# ========== КОНФИГУРАЦИЯ ==========
TG_TOKEN = "8757845092:AAHK3sKNbzy-w7_4oBgHF7p9sYeNGxIXEic"
GEMINI_API_KEY = "AIzaSyDJAiv1aUdtqWv8qbMyvuXhfztCJTSQBtU"
DEEPSEEK_API_KEY = "sk-4c6ad799713d41d8b22f614be5e02264"
TAVILY_API_KEY = "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"

DB_PATH = "simul_core.db"
MAX_IMAGE_SIZE_MB = 20
SEARCH_TIMEOUT = 8.0
PHOTO_DOWNLOAD_TIMEOUT = 10.0
MAX_REPLY_LEN = 4000
MAX_HISTORY_SYMBOLS = 4000

logging.basicConfig(level=logging.INFO)

# ========== КЛИЕНТЫ ==========
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
deepseek_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com/v1"
)

# Инициализируем Tavily (синхронный клиент)
if TAVILY_AVAILABLE:
    tavily_client = TavilyClient(api_key=TAVILY_API_KEY)

# ========== БАЗА ДАННЫХ ==========
class Registration(StatesGroup):
    waiting_for_bot_name = State()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, bot_name TEXT, history TEXT)"
        )
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
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, bot_name, history) VALUES (?, '', '')",
            (user_id,)
        )
        if bot_name is not None:
            await db.execute("UPDATE users SET bot_name = ? WHERE user_id = ?", (bot_name, user_id))
        if history is not None:
            if len(history) > MAX_HISTORY_SYMBOLS:
                history = history[-MAX_HISTORY_SYMBOLS:]
            await db.execute("UPDATE users SET history = ? WHERE user_id = ?", (history, user_id))
        await db.commit()

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

bot = Bot(token=TG_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
search_users = set()

# ========== ПОИСК ЧЕРЕЗ TAVILY (с обёрткой в поток) ==========
async def search_internet(query: str) -> str:
    if not TAVILY_AVAILABLE:
        logging.warning("Tavily not available")
        return ""
    try:
        def sync_search():
            # Возвращает структуру с результатами
            return tavily_client.search(
                query=query,
                search_depth="basic",
                max_results=3,
                include_answer=False
            )
        # Запускаем синхронный поиск в отдельном потоке
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            response = await loop.run_in_executor(pool, sync_search)

        results = response.get("results", [])
        if not results:
            return ""
        formatted = []
        for r in results[:3]:
            title = r.get("title", "")[:100].strip()
            content = r.get("content", "")[:200].strip()
            if title and content:
                formatted.append(f"• {title}\n  {content}\n")
        if formatted:
            return "📱 Информация из интернета:\n" + "\n".join(formatted)
        return ""
    except Exception as e:
        logging.error(f"Tavily search error: {e}")
        return ""

# ========== БЕЗОПАСНАЯ ОТПРАВКА ДЛИННЫХ СООБЩЕНИЙ ==========
async def safe_send(message: aiog_types.Message, text: str, keyboard=None):
    if not text:
        text = "⚠️ Пустой ответ от ИИ."
    text = text.strip()
    if len(text) <= MAX_REPLY_LEN:
        await message.answer(text, reply_markup=keyboard)
        return
    for i in range(0, len(text), MAX_REPLY_LEN):
        part = text[i:i+MAX_REPLY_LEN]
        kb = keyboard if i + MAX_REPLY_LEN >= len(text) else None
        await message.answer(part, reply_markup=kb)
        await asyncio.sleep(0.2)

# ========== МОДЕЛИ ==========
async def ask_gemini(contents):
    try:
        response = await gemini_client.aio.models.generate_content(
            model="gemini-1.5-flash",
            contents=contents
        )
        return response.text.strip() if response.text else None
    except Exception as e:
        logging.error(f"Gemini error: {e}")
        return None

async def ask_deepseek(messages):
    try:
        response = await asyncio.wait_for(
            deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                temperature=0.7,
                max_tokens=1000
            ),
            timeout=10.0
        )
        content = response.choices[0].message.content
        return content.strip() if content else None
    except Exception as e:
        logging.error(f"DeepSeek error: {e}")
        return None

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def cmd_start(message: aiog_types.Message, state: FSMContext):
    user_data = await get_user_data(message.from_user.id)
    if user_data:
        await safe_send(message, f"Simul - BM 100\nВаш ассистент {user_data['bot_name']} готов.", get_main_keyboard())
    else:
        await message.answer("🦾 Протокол Инициализации Simul\n\nОтправьте имя:")
        await state.set_state(Registration.waiting_for_bot_name)

@dp.message(Command("help"))
async def cmd_help(message: aiog_types.Message):
    await safe_send(message,
        "🤖 Справка Simul - BM 100\n\n"
        "/start – запуск\n/help – команды\n\n"
        "Кнопки:\n"
        "• Информация – статус\n"
        "• Очистить память – сброс истории\n"
        "• Изменить имя – задать новое имя\n"
        "• Начать/Закончить поиск – режим поиска\n"
        "• Статус системы – состояние ядра")

@dp.message(Registration.waiting_for_bot_name)
async def process_reg(message: aiog_types.Message, state: FSMContext):
    if message.text and message.text.strip():
        name = message.text.strip()[:20]
        await update_user(message.from_user.id, bot_name=name)
        await state.clear()
        await safe_send(message, f"✅ Готово.\n\nSimul - BM 100\nЯ — {name}.", get_main_keyboard())
    else:
        await message.answer("Пожалуйста, отправьте текстовое имя (не пустое).")

@dp.message(F.text == "ℹ️ Информация")
async def btn_info(message: aiog_types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data:
        await safe_send(message,
            f"🟢 Simul онлайн\nИмя: {user_data['bot_name']}\nПамять: {len(user_data['history'])} симв.",
            get_main_keyboard())
    else:
        await safe_send(message, "Система не инициализирована. Используйте /start", get_main_keyboard())

@dp.message(F.text == "🗑 Очистить память")
async def btn_reset(message: aiog_types.Message):
    await update_user(message.from_user.id, history="")
    await message.answer("🧠 Память очищена.", reply_markup=get_main_keyboard())

@dp.message(F.text == "⚙️ Изменить имя")
async def btn_change_name(message: aiog_types.Message, state: FSMContext):
    await message.answer("Введите новое имя:", reply_markup=aiog_types.ReplyKeyboardRemove())
    await state.set_state(Registration.waiting_for_bot_name)

@dp.message(F.text == "❓ Помощь")
async def btn_help(message: aiog_types.Message):
    await cmd_help(message)

@dp.message(F.text == "📊 Статус системы")
async def btn_sys_status(message: aiog_types.Message):
    user_data = await get_user_data(message.from_user.id)
    if user_data:
        await safe_send(message,
            f"🟢 Simul онлайн\nИмя: {user_data['bot_name']}\nПамять: {len(user_data['history'])} симв.\n"
            f"Режим поиска: {'включён' if message.from_user.id in search_users else 'выключен'}",
            get_main_keyboard())
    else:
        await safe_send(message, "Система не инициализирована. /start", get_main_keyboard())

@dp.message(F.text == "Начать поиск")
async def btn_start_search(message: aiog_types.Message):
    search_users.add(message.from_user.id)
    await message.answer("🔍 Режим поиска активирован. Все запросы будут искать в интернете.", reply_markup=get_main_keyboard())

@dp.message(F.text == "Закончить поиск")
async def btn_end_search(message: aiog_types.Message):
    search_users.discard(message.from_user.id)
    await message.answer("⏹ Режим поиска остановлен.", reply_markup=get_main_keyboard())

# ========== УНИВЕРСАЛЬНЫЙ ОБРАБОТЧИК ==========
@dp.message()
async def universal_handler(message: aiog_types.Message, state: FSMContext):
    if not message.photo and not message.text:
        return

    user_id = message.from_user.id
    user_data = await get_user_data(user_id)

    if not user_data:
        await message.answer("⚠️ Сессия утеряна. Напишите /start", reply_markup=aiog_types.ReplyKeyboardRemove())
        await state.set_state(Registration.waiting_for_bot_name)
        return

    bot_name = user_data["bot_name"]
    history = user_data["history"]
    in_search = user_id in search_users

    persona = f"Ты — Simul, ассистент с именем {bot_name}."
    if in_search:
        persona += " Отвечай кратко и только по существу запроса, используя актуальные данные из поиска."

    async with ChatActionSender.typing(bot=bot, chat_id=message.chat.id):
        try:
            # ---------- ФОТО ----------
            if message.photo:
                photo = message.photo[-1]
                file_info = await bot.get_file(photo.file_id)

                if file_info.file_size and file_info.file_size > MAX_IMAGE_SIZE_MB * 1024 * 1024:
                    await message.answer(f"❌ Изображение превышает {MAX_IMAGE_SIZE_MB} МБ.")
                    return

                try:
                    file_io = io.BytesIO()
                    await asyncio.wait_for(bot.download_file(file_info.file_path, file_io), timeout=PHOTO_DOWNLOAD_TIMEOUT)
                    file_io.seek(0)
                except asyncio.TimeoutError:
                    await message.answer("❌ Таймаут скачивания фото.")
                    return
                except Exception:
                    await message.answer("❌ Не удалось загрузить фото.")
                    return

                try:
                    img = Image.open(file_io)
                    if img.mode in ('RGBA', 'P'):
                        img = img.convert('RGB')
                    img_bytes_io = io.BytesIO()
                    img.save(img_bytes_io, format='JPEG', quality=85)
                    img_bytes = img_bytes_io.getvalue()
                except Exception:
                    await message.answer("❌ Не удалось обработать изображение.")
                    return

                user_text = message.caption if message.caption else "Что на этом фото?"
                context_text = f"{persona}\n\nИстория диалога:\n{history}\n\nПользователь прислал фото и спрашивает: {user_text}"
                image_part = types.Part.from_bytes(data=img_bytes, mime_type='image/jpeg')
                ai_reply = await ask_gemini([context_text, image_part])
                if not ai_reply:
                    ai_reply = "❌ Не удалось получить ответ по этому изображению."

                if not in_search:
                    new_history = f"{history}\nU: [ФОТО] {user_text}\nA: {ai_reply}"
                    if len(new_history) > MAX_HISTORY_SYMBOLS:
                        new_history = new_history[-MAX_HISTORY_SYMBOLS:]
                    await update_user(user_id, history=new_history)

                await safe_send(message, ai_reply, get_main_keyboard())
                return

            # ---------- ТЕКСТ ----------
            user_text = message.text.strip()
            if not user_text:
                return

            # Ключевые слова для авто-поиска
            search_keywords = ["что", "как", "где", "когда", "почему", "какой", "новости", "информация",
                               "событие", "последние", "актуальное", "сейчас", "погода", "курс", "сколько",
                               "кто такой", "что такое", "найти", "расскажи о"]
            need_search = in_search or any(kw in user_text.lower() for kw in search_keywords)

            search_context = ""
            if need_search:
                search_context = await search_internet(user_text)

            # Формируем промпт
            context_text = f"{persona}\n\n"
            if search_context:
                context_text += f"{search_context}\n"
            context_text += f"История диалога:\n{history}\n\nПользователь: {user_text}"

            ai_reply = await ask_gemini([context_text])
            if not ai_reply:
                messages = [{"role": "system", "content": persona}]
                if search_context:
                    messages.append({"role": "system", "content": search_context})
                messages.append({"role": "user", "content": user_text})
                ai_reply = await ask_deepseek(messages)

            if not ai_reply:
                ai_reply = f"Привет, я {bot_name}. Не удалось обработать запрос. Попробуйте позже."

            if not in_search:
                new_history = f"{history}\nU: {user_text}\nA: {ai_reply}"
                if len(new_history) > MAX_HISTORY_SYMBOLS:
                    new_history = new_history[-MAX_HISTORY_SYMBOLS:]
                await update_user(user_id, history=new_history)

            await safe_send(message, ai_reply, get_main_keyboard())

        except Exception as e:
            logging.exception("Critical error in handler")
            await safe_send(message, "⚠️ Системная ошибка. Напишите /start для перезапуска.", get_main_keyboard())

async def main():
    await init_db()
    logging.info("✅ Бот успешно запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())