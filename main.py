import os
import sqlite3
import threading
from flask import Flask
import telebot
from telebot import types
from openai import OpenAI
import requests
import io

# --- ЗАГЛУШКА ДЛЯ ВЕБ-ПОРТА RENDER ---
app = Flask('')

@app.route('/')
def home():
    return "ИИННОКЕНТИЙ работает 24/7!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

PROMO_TEXT_VIP = os.environ.get("PROMO_TEXT_VIP", "VIP2026").strip().upper()
PROMO_IMAGE_ACCESS = os.environ.get("ALLOW_IMAGES_PROMO", "ART2026").strip().upper()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

SUPPORT_ADMIN = os.environ.get("SUPPORT_ADMIN", "@admin")
SUPPORT_CHANNEL = os.environ.get("SUPPORT_CHANNEL", "https://t.me/telegram")

# --- ИНИЦИАЛИЗАЦИЯ OPENAI И БОТА ---
client = OpenAI(api_key=OPENAI_API_KEY)
bot = telebot.TeleBot(TELEGRAM_TOKEN)

# --- БАЗА ДАННЫХ SQLite ---
DB_FILE = "bot_database.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            has_text_vip INTEGER DEFAULT 0,
            has_image_vip INTEGER DEFAULT 0,
            ai_mode TEXT DEFAULT 'general'
        )
    ''')
    conn.commit()
    conn.close()

def get_user(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT has_text_vip, has_image_vip, ai_mode FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"text_vip": bool(row[0]), "image_vip": bool(row[1]), "mode": row[2]}
    return {"text_vip": False, "image_vip": False, "mode": "general"}

def add_or_update_user(user_id, username, text_vip=None, image_vip=None, mode=None):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT has_text_vip, has_image_vip, ai_mode FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    if row is None:
        t_vip = 1 if text_vip else 0
        i_vip = 1 if image_vip else 0
        m = mode if mode else 'general'
        cursor.execute(
            "INSERT INTO users (user_id, username, has_text_vip, has_image_vip, ai_mode) VALUES (?, ?, ?, ?, ?)",
            (user_id, username, t_vip, i_vip, m)
        )
    else:
        t_vip = (1 if text_vip else row[0]) if text_vip is not None else row[0]
        i_vip = (1 if image_vip else row[1]) if image_vip is not None else row[1]
        m = mode if mode is not None else row[2]
        cursor.execute(
            "UPDATE users SET username = ?, has_text_vip = ?, has_image_vip = ?, ai_mode = ? WHERE user_id = ?",
            (username, t_vip, i_vip, m, user_id)
        )
    conn.commit()
    conn.close()

def get_all_users():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]

init_db()

# Хранилище истории сообщений в памяти для сохранения контекста
user_histories = {}

def get_ai_response(user_id, user_message, is_vip, mode):
    system_prompt = "Тебя зовут ИИННОКЕНТИЙ. Ты вежливый, мудрый и находчивый ИИ-ассистент."
    if mode == "coder":
        system_prompt = "Тебя зовут ИИННОКЕНТИЙ. Ты эксперт по программированию. Отвечай точным кодом и понятными пояснениями."
    elif mode == "writer":
        system_prompt = "Тебя зовут ИИННОКЕНТИЙ. Ты креативный копирайтер. Пиши ярко, структурированно и увлекательно."

    # Модель: gpt-4o для VIP, gpt-4o-mini для обычной версии
    model_name = "gpt-4o" if is_vip else "gpt-4o-mini"

    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": system_prompt}]

    user_histories[user_id].append({"role": "user", "content": user_message})

    # Ограничиваем историю 10 последними сообщениями, чтобы не расходовать лишние токены
    messages = [user_histories[user_id][0]] + user_histories[user_id][-10:]

    response = client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=0.7
    )

    answer = response.choices[0].message.content
    user_histories[user_id].append({"role": "assistant", "content": answer})
    return answer

# --- КЛАВИАТУРЫ ---
def get_main_keyboard(user_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_ai = types.KeyboardButton("🤖 Задать вопрос ИИ")
    btn_image = types.KeyboardButton("🖼 Создать картинку")
    btn_modes = types.KeyboardButton("🎭 Режимы ИИ")
    btn_clear = types.KeyboardButton("🧹 Очистить диалог")
    btn_buy = types.KeyboardButton("⭐ VIP / Stars")
    btn_info = types.KeyboardButton("ℹ️ Статус")
    btn_support = types.KeyboardButton("🛠 Поддержка")
    
    markup.add(btn_ai, btn_image)
    markup.add(btn_modes, btn_clear)
    markup.add(btn_buy, btn_info, btn_support)
    
    if user_id == ADMIN_ID:
        markup.add(types.KeyboardButton("👑 Админ Панель"))
    return markup

# --- КОМАНДА /START ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    username = message.from_user.username or "Anonymous"
    add_or_update_user(user_id, username)

    welcome_text = (
        f"Привет, {message.from_user.first_name}! 👋\n\n"
        "Я **ИИННОКЕНТИЙ** — твой личный ИИ-помощник на базе ChatGPT!\n"
        "Задай мне любой вопрос, попроси написать код или создать картинку.\n\n"
        "Выбирай нужный раздел в меню ниже 👇"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard(user_id), parse_mode="Markdown")

# --- ОБРАБОТЧИК МЕНЮ И СООБЩЕНИЙ ---
@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    user_id = message.from_user.id
    text = message.text
    user_data = get_user(user_id)

    if text == "🧹 Очистить диалог":
        if user_id in user_histories:
            del user_histories[user_id]
        bot.send_message(message.chat.id, "🧹 Память нашего диалога очищена!")

    elif text == "🎭 Режимы ИИ":
        markup = types.InlineKeyboardMarkup()
        btn1 = types.InlineKeyboardButton("🌐 Обычный", callback_data="set_mode_general")
        btn2 = types.InlineKeyboardButton("💻 Программист", callback_data="set_mode_coder")
        btn3 = types.InlineKeyboardButton("✍️ Копирайтер", callback_data="set_mode_writer")
        markup.add(btn1, btn2, btn3)
        bot.send_message(message.chat.id, f"Текущая роль ИИннокентия: **{user_data['mode']}**\nВыберите новый режим:", reply_markup=markup, parse_mode="Markdown")

    elif text == "⭐ VIP / Stars":
        markup = types.InlineKeyboardMarkup()
        btn_text = types.InlineKeyboardButton("⭐️ VIP Текст (GPT-4o) - 50 Stars", callback_data="buy_text_vip")
        btn_img = types.InlineKeyboardButton("🎨 VIP Картинки - 100 Stars", callback_data="buy_image_vip")
        markup.add(btn_text)
        markup.add(btn_img)
        bot.send_message(message.chat.id, "Покупка подписок через **Telegram Stars** (или активируйте промокод в профиле):", reply_markup=markup, parse_mode="Markdown")

    elif text == "ℹ️ Статус":
        status_text = "⭐️ **VIP (GPT-4o)**" if user_data["text_vip"] else "👤 **Standard (GPT-4o-mini)**"
        status_image = "✅ **Открыт**" if user_data["image_vip"] else "❌ **Закрыт**"
        
        info_text = (
            "ℹ️ **Ваш профиль**\n\n"
            f"• Модель текста: {status_text}\n"
            f"• Доступ к картинкам: {status_image}\n"
            f"• Режим: `{user_data['mode']}`\n\n"
            f"🆔 Ваш ID: `{user_id}`"
        )
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🎟 Ввести промокод", callback_data="enter_promo"))
        bot.send_message(message.chat.id, info_text, parse_mode="Markdown", reply_markup=markup)

    elif text == "👑 Админ Панель" and user_id == ADMIN_ID:
        users_count = len(get_all_users())
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📢 Сделать рассылку", callback_data="admin_broadcast"))
        bot.send_message(message.chat.id, f"👑 **Админ Панель**\n\nВсего пользователей: `{users_count}`", parse_mode="Markdown", reply_markup=markup)

    elif text == "🛠 Поддержка":
        support_text = f"🛠 **Служба поддержки**\n\n• Админ: {SUPPORT_ADMIN}\n• Канал: {SUPPORT_CHANNEL}"
        bot.send_message(message.chat.id, support_text, parse_mode="Markdown", disable_web_page_preview=True)

    elif text == "🖼 Создать картинку":
        if user_data["image_vip"]:
            msg = bot.send_message(message.chat.id, "🖌 **Опишите картинку:**", parse_mode="Markdown")
            bot.register_next_step_handler(msg, generate_art)
        else:
            bot.send_message(message.chat.id, "🔒 Купите VIP или введите промокод (`ART2026`) в профиле!", parse_mode="Markdown")

    elif text == "🤖 Задать вопрос ИИ":
        bot.send_message(message.chat.id, "Задавай любой вопрос прямо в чат!")

    else:
        try:
            bot.send_chat_action(message.chat.id, 'typing')
            answer = get_ai_response(user_id, text, user_data["text_vip"], user_data["mode"])
            prefix = "⭐️ [VIP ChatGPT]:\n\n" if user_data["text_vip"] else "👴 [ИИннокентий]:\n\n"
            bot.reply_to(message, prefix + answer)
        except Exception as e:
            bot.reply_to(message, f"⚠️ Ошибка OpenAI API: `{str(e)}`", parse_mode="Markdown")

# --- CALLBACKS И ПОКУПКИ ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = call.from_user.id
    
    if call.data.startswith("set_mode_"):
        new_mode = call.data.replace("set_mode_", "")
        add_or_update_user(user_id, call.from_user.username, mode=new_mode)
        if user_id in user_histories:
            del user_histories[user_id]
        bot.answer_callback_query(call.id, "Режим изменен!")
        bot.edit_message_text(f"✅ Режим изменен на: **{new_mode}**", call.message.chat.id, call.message.message_id, parse_mode="Markdown")

    elif call.data == "enter_promo":
        msg = bot.send_message(call.message.chat.id, "🎟 Введите ваш промокод:")
        bot.register_next_step_handler(msg, process_promo_code)

    elif call.data == "admin_broadcast" and user_id == ADMIN_ID:
        msg = bot.send_message(call.message.chat.id, "📢 Введите текст рассылки:")
        bot.register_next_step_handler(msg, process_broadcast)

    elif call.data in ["buy_text_vip", "buy_image_vip"]:
        send_stars_invoice(call)

def process_broadcast(message):
    users = get_all_users()
    success = 0
    bot.send_message(message.chat.id, "⏳ Рассылка запущена...")
    for uid in users:
        try:
            bot.send_message(uid, f"📢 **Сообщение от администрации:**\n\n{message.text}", parse_mode="Markdown")
            success += 1
        except Exception:
            pass
    bot.send_message(message.chat.id, f"✅ Рассылка завершена. Доставлено: {success}/{len(users)}")

def process_promo_code(message):
    user_id = message.from_user.id
    username = message.from_user.username or "Anonymous"
    user_code = message.text.strip().upper()

    activated = False
    if user_code == PROMO_TEXT_VIP:
        add_or_update_user(user_id, username, text_vip=True)
        bot.send_message(message.chat.id, "🎉 Текстовый VIP активирован!")
        activated = True
    if user_code == PROMO_IMAGE_ACCESS:
        add_or_update_user(user_id, username, image_vip=True)
        bot.send_message(message.chat.id, "🎉 Доступ к картинкам активирован!")
        activated = True

    if not activated:
        bot.send_message(message.chat.id, "❌ Неверный промокод.")

def send_stars_invoice(call):
    if call.data == "buy_text_vip":
        title = "VIP Доступ (GPT-4o)"
        description = "Улучшенная нейросеть GPT-4o."
        payload = "payload_text_vip"
        amount = 50
    else:
        title = "VIP Доступ (Картинки)"
        description = "Генерация картинок без ограничений."
        payload = "payload_image_vip"
        amount = 100

    bot.send_invoice(
        call.message.chat.id, title=title, description=description,
        invoice_payload=payload, provider_token="", currency="XTR",
        prices=[types.LabeledPrice(label=title, amount=amount)], start_parameter="vip-buy"
    )

@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def got_payment(message):
    user_id = message.from_user.id
    username = message.from_user.username or "Anonymous"
    payload = message.successful_payment.invoice_payload

    if payload == "payload_text_vip":
        add_or_update_user(user_id, username, text_vip=True)
        bot.send_message(message.chat.id, "🎉 VIP-текст (GPT-4o) активирован!")
    elif payload == "payload_image_vip":
        add_or_update_user(user_id, username, image_vip=True)
        bot.send_message(message.chat.id, "🎉 Генерация картинок активирована!")

def generate_art(message):
    user_id = message.from_user.id
    prompt = message.text
    user_data = get_user(user_id)

    if not user_data["image_vip"]:
        bot.send_message(message.chat.id, "❌ Нет доступа.")
        return

    wait_msg = bot.send_message(message.chat.id, "🎨 **ИИннокентий рисует...**", parse_mode="Markdown")
    bot.send_chat_action(message.chat.id, 'upload_photo')

    try:
        encoded_prompt = requests.utils.quote(prompt)
        image_url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&private=true"
        response = requests.get(image_url)
        
        if response.status_code == 200:
            photo = io.BytesIO(response.content)
            bot.send_photo(message.chat.id, photo, caption=f"🖌 Арт по запросу: `{prompt}`", parse_mode="Markdown")
            bot.delete_message(message.chat.id, wait_msg.message_id)
        else:
            bot.edit_message_text("⚠️ Ошибка сервера картинок.", message.chat.id, wait_msg.message_id)
    except Exception:
        bot.edit_message_text("⚠️ Ошибка генерации.", message.chat.id, wait_msg.message_id)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    bot.infinity_polling()
