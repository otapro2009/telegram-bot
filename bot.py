import os
import time
import threading
import asyncio
from functools import wraps
from flask import Flask
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest
from supabase import create_client, Client
import logging
import requests
from urllib.parse import urlparse

# ───────────────────────────────────────────────
# إعداد السجل (Logging)
# ───────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ───────────────────────────────────────────────
# تهيئة المتغيرات والعملاء
# ───────────────────────────────────────────────
DEVELOPER_USERNAME = "@ota_m_pro"
ADMIN_ID = 8504617214

BOT_TOKEN = os.environ.get("BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
OPENROUTER_API_KEY = (os.environ.get("OPENROUTER_API_KEY") or "").strip()

if not BOT_TOKEN or not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError(
        "يرجى التأكد من ضُبط BOT_TOKEN و SUPABASE_URL و SUPABASE_KEY في متغيرات البيئة!"
    )

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

if not OPENROUTER_API_KEY:
    logger.warning("OPENROUTER_API_KEY is missing; AI service is disabled.")

# ───────────────────────────────────────────────
# خادم ويب وهمي (للـ Health Check على Render)
# ───────────────────────────────────────────────
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run)
    t.start()


# ───────────────────────────────────────────────
# نظام الحماية من السبام (مع تنظيف تلقائي)
# ───────────────────────────────────────────────
user_last_message_time = {}
SPAM_INTERVAL = 0.3
SPAM_CLEANUP_INTERVAL = 3600  # تنظيف كل ساعة


def check_spam(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return False

    current_time = time.time()

    # تنظيف المستخدمين القدامى (تسريب الذاكرة)
    cutoff = current_time - SPAM_CLEANUP_INTERVAL
    for uid, last in list(user_last_message_time.items()):
        if last < cutoff:
            user_last_message_time.pop(uid, None)

    last_time = user_last_message_time.get(user_id, 0)
    if current_time - last_time < SPAM_INTERVAL:
        return True

    user_last_message_time[user_id] = current_time
    return False


# ───────────────────────────────────────────────
# التخزين المؤقت المحلي (Caching)
# ───────────────────────────────────────────────
local_cache = {
    "subjects": {},
    "sections": {},
    "lessons": {},
    "notes": {},
}


def clear_user_cache(user_id: int):
    local_cache["subjects"].pop(user_id, None)
    for key in list(local_cache["sections"].keys()):
        if key.startswith(f"{user_id}_"):
            local_cache["sections"].pop(key, None)
    for key in list(local_cache["lessons"].keys()):
        if key.startswith(f"{user_id}_"):
            local_cache["lessons"].pop(key, None)
    for key in list(local_cache["notes"].keys()):
        if key.startswith(f"{user_id}_"):
            local_cache["notes"].pop(key, None)


# ───────────────────────────────────────────────
# دوال مساعدة لـ Supabase (غير المتزامنة)
# ───────────────────────────────────────────────
async def sb_execute(func):
    """تشغيل عمليات Supabase المتزامنة في thread منفصل."""
    return await asyncio.to_thread(func)


# ───────────────────────────────────────────────
# Decorator للأوامر المخصصة للمطور
# ───────────────────────────────────────────────
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id != ADMIN_ID:
            if update.message:
                await update.message.reply_text("هذا الأمر مخصص للمطور فقط ❌")
            return
        return await func(update, context)
    return wrapper


# ───────────────────────────────────────────────
# معالج الأخطاء العام
# ───────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "عذراً، حدث خطأ غير متوقع. يرجى المحاولة لاحقاً."
            )
        except Exception as e:
            logger.error(f"Failed to send error message to user: {e}")


# ───────────────────────────────────────────────
# أوامر البوت (تظهر في قائمة /)
# ───────────────────────────────────────────────
async def post_init(application):
    await application.bot.set_my_commands([
        BotCommand("start", "القائمة الرئيسية 🏠"),
        BotCommand("guide", "توجيه وطريقة استخدام البوت 🧭"),
        BotCommand("lessons", "عرض موادي ودروسي 📚"),
        BotCommand("tasks", "المهام اليومية 📅"),
        BotCommand("help", "مساعدة ℹ️"),
    ])


# ───────────────────────────────────────────────
# /start
# ───────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    user_id = user.id
    if check_spam(user_id):
        return

    username = f"@{user.username}" if user.username else "بدون يوزر"
    first_name = user.first_name or "مستخدم"

    try:
        res = await sb_execute(
            lambda: supabase.table("users")
            .select("welcomed")
            .eq("user_id", user_id)
            .execute()
        )
        data = res.data
    except Exception:
        logger.exception("Exception caught in start()")
        data = []

    if not data:
        try:
            await sb_execute(
                lambda: supabase.table("users").insert({
                    "user_id": user_id,
                    "username": username,
                    "first_name": first_name,
                    "welcomed": 0,
                }).execute()
            )
        except Exception:
            logger.exception("Exception caught in start()")
            pass
        try:
            admin_msg = (
                f"🔔 عضو جديد انضم للبوت!\n\n"
                f"👤 الاسم: {first_name}\n"
                f"🆔 المعرف: {username}\n"
                f"🔢 الـ ID: `{user_id}`"
            )
            await context.bot.send_message(
                chat_id=ADMIN_ID, text=admin_msg, parse_mode="Markdown"
            )
        except Exception:
            logger.exception("Exception caught in start()")
            pass
        is_first_time = True
    else:
        is_first_time = data[0].get("welcomed") == 0

    keyboard = [
        [InlineKeyboardButton("📚 عرض موادي ودروسي", callback_data="show_subjects")],
        [InlineKeyboardButton("📅 المهام اليومية", callback_data="show_tasks")],
        [InlineKeyboardButton("🧭 التوجيه وطريقة الاستخدام", callback_data="show_guide")],
        [
            InlineKeyboardButton(
                "💬 مراسلة المطور",
                url=f"https://t.me/{DEVELOPER_USERNAME.replace('@', '')}",
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if is_first_time:
        try:
            await sb_execute(
                lambda: supabase.table("users")
                .update({"welcomed": 1})
                .eq("user_id", user_id)
                .execute()
            )
        except Exception:
            logger.exception("Exception caught in start()")
            pass
        welcome_text = (
            f"👋 أهلاً بك يا {first_name} في بوت BAC 2027 المطور!\n\n"
            "📌 **طريقة استخدام البوت:**\n"
            "• انقر على 'عرض موادي ودروسي' لإدارة جدولك، دروسك، وملاحظات كل مادة.\n"
            "• تنظيم خطتك ودراستك اليومية عبر 'المهام اليومية'.\n\n"
            "اختر من القائمة أدناه للبدء:"
        )
    else:
        welcome_text = (
            f"👋 أهلاً بك مجدداً يا {first_name}!\n\n"
            "مساحتك الشخصية لتنظيم دروسك، مهامك اليومية، وملاحظات موادك 📚✨.\n"
            "اختر من القائمة أدناه:"
        )

    if update.message:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup)
    elif update.callback_query:
        await update.callback_query.message.edit_text(
            welcome_text, reply_markup=reply_markup
        )


# ───────────────────────────────────────────────
# /lessons
# ───────────────────────────────────────────────
async def lessons_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or check_spam(user.id):
        return
    await show_subjects_menu(update, context)


# ───────────────────────────────────────────────
# /tasks
# ───────────────────────────────────────────────
async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or check_spam(user.id):
        return
    await show_tasks_menu(update, context)


# ───────────────────────────────────────────────
# /guide و /help
# ───────────────────────────────────────────────
GUIDE_TEXT = (
    "🧭 توجيه — طريقة استخدام بوت OTA / Bac2027\n\n"
    "🏠 القائمة الرئيسية:\n"
    "• 📚 عرض موادي ودروسي: لإضافة المواد وتنظيم فروعها ودروسها وملاحظاتها.\n"
    "• 📅 المهام اليومية: لإضافة المهام وإدارتها وعرض المنجزة والمؤجلة.\n"
    "• 🧭 التوجيه: لقراءة هذا الدليل.\n"
    "• 💬 مراسلة المطور: لاقتراح ميزة أو الإبلاغ عن مشكلة.\n\n"
    "📚 المواد والدروس:\n"
    "1. اضغط «إضافة مادة جديدة» ثم أرسل اسم المادة.\n"
    "2. افتح المادة لإضافة الفروع أو إدارة ملاحظاتها، أو حذف المادة.\n"
    "3. افتح «الفروع والدروس» ثم أضف فرعًا أو افتحه.\n"
    "4. داخل الفرع اضغط «إضافة درس جديد»، أرسل عنوانه، ثم أرسل محتواه.\n"
    "5. يمكن أن يكون المحتوى نصًا أو رابط HTTP/HTTPS أو صورة أو فيديو أو ملفًا أو صوتًا/رسالة صوتية أو رسومًا متحركة أو ملصقًا أو مرفقًا مدعومًا من تيليجرام.\n"
    "6. افتح الدرس لعرض محتواه، وتعديل عنوانه، أو الرجوع إلى قائمة الدروس.\n"
    "7. من «ملاحظات هذه المادة» يمكنك إضافة ملاحظة أو فتح ملاحظة محفوظة أو حذفها.\n\n"
    "📅 المهام اليومية:\n"
    "• اضغط «إضافة مهمة جديدة» ثم أرسل نص المهمة كاملًا.\n"
    "• ✅ إنجاز: ينقل المهمة إلى قائمة المهام المنجزة.\n"
    "• ⏳ تأجيل: ينقل المهمة إلى قائمة المهام المؤجلة.\n"
    "• 🗑️ حذف: يحذف المهمة.\n"
    "• افتح «المهام المنجزة» للرجوع إلى المهام التي أنهيتها، أو «المهام المؤجلة» لاستعادتها لاحقًا.\n\n"
    "🤖 يمكنك إرسال سؤال دراسي عادي إلى المساعد الذكي إذا كانت الخدمة مفعّلة.\n"
    "إذا كانت هناك ميزة أو وظيفة تريد إضافتها وغير متاحة حاليًا، راسل المطور @ota_m_pro واقترحها عليه. لن يقدّم لك البوت أوامر غير موجودة على أنها تعمل."
)

async def guide_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]
    ])
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(
                GUIDE_TEXT, reply_markup=keyboard
            )
        except Exception:
            logger.exception("Could not edit message to display guide")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=GUIDE_TEXT,
                reply_markup=keyboard,
            )
    elif update.message:
        await update.message.reply_text(GUIDE_TEXT, reply_markup=keyboard)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await guide_command(update, context)


# ───────────────────────────────────────────────
# أوامر المطور
# ───────────────────────────────────────────────
@admin_only
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        res = await sb_execute(
            lambda: supabase.table("users").select("user_id", count="exact").execute()
        )
        total_users = res.count if res.count is not None else len(res.data)
    except Exception:
        logger.exception("Exception caught in stats()")
        total_users = 0
    await update.message.reply_text(
        f"📊 إحصائيات البوت:\nعدد الأعضاء المسجلين: {total_users}"
    )


@admin_only
async def list_all_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        res = await sb_execute(
            lambda: supabase.table("users")
            .select("user_id, username, first_name")
            .execute()
        )
        users = res.data
    except Exception:
        logger.exception("Exception caught in list_all_users()")
        users = []

    if not users:
        await update.message.reply_text("لا توجد أي أعضاء مسجلين حتى الآن.")
        return

    text = "👥 قائمة الأعضاء المسجلين في البوت:\n\n"
    for idx, u in enumerate(users, 1):
        u_id = u.get("user_id")
        username = u.get("username")
        first_name = u.get("first_name") or "مستخدم"
        user_link = (
            f"[{first_name}](https://t.me/{username.replace('@', '')})"
            if username and username != "بدون يوزر"
            else f"{first_name} (بدون معرف)"
        )
        text += f"{idx}. {user_link} — `[ID: {u_id}]`\n"
        if len(text) > 3500:
            await update.message.reply_text(text, parse_mode="Markdown")
            text = ""
    if text:
        await update.message.reply_text(text, parse_mode="Markdown")


@admin_only
async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "❌ يرجى كتابة النص بعد الأمر هكذا:\n`/broadcast [الرسالة]`",
            parse_mode="Markdown",
        )
        return

    announcement = " ".join(context.args)
    success_count = fail_count = 0
    try:
        res = await sb_execute(
            lambda: supabase.table("users").select("user_id").execute()
        )
        users = res.data
    except Exception:
        logger.exception("Exception caught in broadcast_message()")
        users = []

    for u in users:
        u_id = u.get("user_id")
        try:
            await context.bot.send_message(
                chat_id=u_id,
                text=announcement
            )
            success_count += 1
        except Exception as e:
            fail_count += 1
            logger.exception("Failed to send broadcast message to a user")
    await asyncio.sleep(0.05)

    await update.message.reply_text(
        f"📢 تم الإرسال بنجاح!\n✅ وصل إلى: {success_count}\n❌ فشل: {fail_count}"
    )


# ───────────────────────────────────────────────
# القوائم التفاعلية
# ───────────────────────────────────────────────
async def show_subjects_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    user_id = user.id

    if user_id in local_cache["subjects"]:
        subjects = local_cache["subjects"][user_id]
    else:
        try:
            res = await sb_execute(
                lambda: supabase.table("subjects")
                .select("subject_name")
                .eq("user_id", user_id)
                .execute()
            )
            subjects = res.data
        except Exception:
            logger.exception("Exception caught in show_subjects_menu()")
            subjects = []
        local_cache["subjects"][user_id] = subjects

    text = "📚 قائمة موادي الدراسية:\nاختر المادة لتصفح فروعها، ملاحظاتها أو إدارتها:"
    keyboard = []
    if subjects:
        for item in subjects:
            sub = item.get("subject_name")
            keyboard.append([InlineKeyboardButton(f"📖 {sub}", callback_data=f"sub|{sub}")])
    keyboard.append([InlineKeyboardButton("➕ إضافة مادة جديدة", callback_data="add_subject")])
    keyboard.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=reply_markup)
        except Exception:
            logger.exception("Exception caught in show_subjects_menu()")
            await context.bot.send_message(
                chat_id=update.effective_chat.id, text=text, reply_markup=reply_markup
            )
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def show_subject_options(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, subject: str
):
    text = f"📖 **مادة: {subject}**\nاختر القسم الذي تريد تصفحه أو إدارته:"
    keyboard = [
        [
            InlineKeyboardButton(
                "📂 الفروع والدروس", callback_data=f"show_sec_list|{subject}"
            )
        ],
        [
            InlineKeyboardButton(
                "📝 ملاحظات هذه المادة", callback_data=f"show_sub_notes|{subject}"
            )
        ],
        [InlineKeyboardButton("حذف المادة 🗑️", callback_data=f"delsub|{subject}")],
        [
            InlineKeyboardButton(
                "🔙 رجوع للمواد", callback_data="show_subjects"
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.message.edit_text(
            text, reply_markup=reply_markup, parse_mode="Markdown"
        )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )


async def show_sections_menu_direct(update, context, user_id, subject):
    cache_key = f"{user_id}_{subject}"
    if cache_key in local_cache["sections"]:
        sections = local_cache["sections"][cache_key]
    else:
        try:
            res = await sb_execute(
                lambda: supabase.table("sections")
                .select("section_name")
                .eq("user_id", user_id)
                .eq("subject_name", subject)
                .execute()
            )
            sections = res.data
        except Exception:
            logger.exception("Exception caught in show_sections_menu_direct()")
            sections = []
        local_cache["sections"][cache_key] = sections

    keyboard = []
    if sections:
        text = f"📚 مادة: {subject}\nاختر الفرع المطلوب:"
        for item in sections:
            sec = item.get("section_name")
            keyboard.append([
                InlineKeyboardButton(f"📂 فرع: {sec}", callback_data=f"sec|{subject}|{sec}"),
                InlineKeyboardButton(
                    "حذف الفرع 🗑️", callback_data=f"delsec|{subject}|{sec}"
                ),
            ])
    else:
        text = f"📚 مادة: {subject}\n❌ أنت لا تملك أي فروع في هذه المادة حالياً."

    keyboard.append([
        InlineKeyboardButton(
            "➕ إضافة فرع جديد", callback_data=f"addsec|{subject}"
        )
    ])
    keyboard.append(
        [InlineKeyboardButton("🔙 رجوع خيارات المادة", callback_data=f"sub|{subject}")]
    )

    if update.callback_query:
        await update.callback_query.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def show_lessons_menu_direct(update, context, user_id, subject, section):
    cache_key = f"{user_id}_{subject}_{section}"
    if cache_key in local_cache["lessons"]:
        lessons = local_cache["lessons"][cache_key]
    else:
        try:
            res = await sb_execute(
                lambda: supabase.table("lessons")
                .select("id, title, content_type")
                .eq("user_id", user_id)
                .eq("subject_name", subject)
                .eq("section_name", section)
                .execute()
            )
            lessons = res.data
        except Exception:
            logger.exception("Exception caught in show_lessons_menu_direct()")
            lessons = []
        local_cache["lessons"][cache_key] = lessons

    keyboard = []
    if lessons:
        text = f"📂 مادة: {subject} ➔ فرع: {section}\nقائمة الدروس المحفوظة:"
        for item in lessons:
            lesson_id = item.get("id")
            title = item.get("title")
            c_type = item.get("content_type")
            type_label = {
                "video": "🎥",
                "photo": "🖼️",
                "document": "📄",
                "link": "🔗",
                "audio": "🎧",
                "voice": "🎙️",
                "video_note": "⭕",
                "animation": "🎞️",
                "sticker": "🧩",
                "text": "📝",
            }.get(c_type, "📎")
            keyboard.append([
                InlineKeyboardButton(
                    f"{type_label} {title}", callback_data=f"op|{lesson_id}"
                ),
                InlineKeyboardButton("حذف 🗑️", callback_data=f"dl|{lesson_id}"),
            ])
    else:
        text = f"📂 مادة: {subject} ➔ فرع: {section}\n❌ أنت لا تملك أي دروس في هذا الفرع."

    keyboard.append([
        InlineKeyboardButton(
            "➕ إضافة درس جديد", callback_data=f"addles|{subject}|{section}"
        )
    ])
    keyboard.append(
        [
            InlineKeyboardButton(
                "🔙 رجوع للفروع", callback_data=f"show_sec_list|{subject}"
            )
        ]
    )

    if update.callback_query:
        message = update.callback_query.message
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Telegram cannot edit a photo/video/document message with edit_text.
        # When returning from a media lesson, remove that message and send the lessons list.
        if (
            message.photo or message.video or message.document or message.audio
            or message.voice or message.video_note or message.animation or message.sticker
        ):
            try:
                await message.delete()
            except Exception:
                logger.exception(
                    "Could not delete media lesson message while returning to lessons."
                )
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                reply_markup=reply_markup,
            )
        else:
            try:
                await message.edit_text(text, reply_markup=reply_markup)
            except Exception:
                logger.exception(
                    "Could not edit lessons message; sending a new lessons list instead."
                )
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=text,
                    reply_markup=reply_markup,
                )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def show_subject_notes_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, subject: str
):
    cache_key = f"{user_id}_{subject}"
    if cache_key in local_cache["notes"]:
        notes = local_cache["notes"][cache_key]
    else:
        try:
            res = await sb_execute(
                lambda: supabase.table("subject_notes")
                .select("id, note_text")
                .eq("user_id", user_id)
                .eq("subject_name", subject)
                .order("id", desc=True)
                .execute()
            )
            notes = res.data
        except Exception:
            logger.exception("Exception caught in show_subject_notes_menu()")
            notes = []
        local_cache["notes"][cache_key] = notes

    text = f"📝 **ملاحظات مادة [{subject}]:**\n\n"
    keyboard = []
    if notes:
        for n in notes:
            n_id = n.get("id")
            n_text = n.get("note_text")
            display_text = n_text[:25] + "..." if len(n_text) > 25 else n_text
            keyboard.append([
                InlineKeyboardButton(
                    f"📌 {display_text}", callback_data=f"view_sub_note|{n_id}"
                ),
                InlineKeyboardButton(
                    "حذف 🗑️", callback_data=f"del_sub_note|{n_id}|{subject}"
                ),
            ])
    else:
        text += "لا توجد أي ملاحظات محفوظة لهذه المادة حالياً."

    keyboard.append([
        InlineKeyboardButton(
            "➕ إضافة ملاحظة للمادة", callback_data=f"add_sub_note|{subject}"
        )
    ])
    keyboard.append(
        [InlineKeyboardButton("🔙 رجوع خيارات المادة", callback_data=f"sub|{subject}")]
    )

    if update.callback_query:
        await update.callback_query.message.edit_text(
            text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
        )
    else:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )


TASK_VIEW_MESSAGE_IDS_KEY = "_task_view_message_ids"


async def _clear_task_view_messages(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    message_ids = context.user_data.pop(TASK_VIEW_MESSAGE_IDS_KEY, [])
    for message_id in message_ids:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            # قد تكون بعض الرسائل حذفت مسبقًا؛ لا نوقف عرض القائمة بسبب ذلك.
            logger.debug("Could not delete an old task-view message %s", message_id)


async def _remember_task_message(context: ContextTypes.DEFAULT_TYPE, message):
    message_ids = context.user_data.setdefault(TASK_VIEW_MESSAGE_IDS_KEY, [])
    message_ids.append(message.message_id)


async def show_tasks_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    view: str = "active",
):
    user = update.effective_user
    if not user:
        return
    user_id = user.id

    try:
        query = (
            supabase.table("tasks")
            .select("id, task_text, is_completed, is_postponed")
            .eq("user_id", user_id)
        )
        if view == "completed":
            query = query.eq("is_completed", True)
            title = "✅ مهامك المنجزة المحفوظة:"
        elif view == "postponed":
            query = query.eq("is_completed", False).eq("is_postponed", True)
            title = "⏳ مهامك المؤجلة:"
        else:
            query = query.eq("is_completed", False).eq("is_postponed", False)
            title = "📅 مهامك اليومية:"
        res = await sb_execute(lambda: query.order("id", desc=False).execute())
        tasks = res.data or []
    except Exception:
        logger.exception("Could not load task list (view=%s, user_id=%s)", view, user_id)
        message_text = "⚠️ تعذر تحميل المهام من قاعدة البيانات الآن. لم نعتبر القائمة فارغة؛ حاول مجددًا لاحقًا."
        if update.callback_query:
            await update.callback_query.message.reply_text(message_text)
        elif update.message:
            await update.message.reply_text(message_text)
        return

    chat_id = update.effective_chat.id
    tracked_ids_before = set(context.user_data.get(TASK_VIEW_MESSAGE_IDS_KEY, []))
    await _clear_task_view_messages(context, chat_id)

    # إذا جاء الانتقال من زر ليس ضمن رسائل قائمة المهام، احذف لوحة الزر القديمة وحدها.
    if update.callback_query:
        old_message = update.callback_query.message
        if old_message.message_id not in tracked_ids_before:
            try:
                await old_message.delete()
            except Exception:
                logger.debug("Could not delete previous task navigation message")

    header = await context.bot.send_message(chat_id=chat_id, text=title)
    await _remember_task_message(context, header)

    if not tasks:
        empty_text = {
            "completed": "لا توجد مهام منجزة حتى الآن.",
            "postponed": "لا توجد مهام مؤجلة حاليًا.",
        }.get(view, "لا توجد مهام يومية غير منجزة حاليًا.")
        sent = await context.bot.send_message(chat_id=chat_id, text=empty_text)
        await _remember_task_message(context, sent)

    for task in tasks:
        task_id = task.get("id")
        task_text = str(task.get("task_text") or "(مهمة بدون اسم)")
        if view == "active":
            buttons = [[
                InlineKeyboardButton("✅", callback_data=f"complete_task|{task_id}"),
                InlineKeyboardButton("⏳", callback_data=f"postpone_task|{task_id}"),
                InlineKeyboardButton("🗑️", callback_data=f"del_task|{task_id}"),
            ]]
            display_text = task_text
        elif view == "postponed":
            buttons = [[
                InlineKeyboardButton("↩️", callback_data=f"restore_task|{task_id}"),
                InlineKeyboardButton("✅", callback_data=f"complete_task|{task_id}"),
                InlineKeyboardButton("🗑️", callback_data=f"del_task|{task_id}"),
            ]]
            display_text = f"⏳ {task_text}"
        else:
            buttons = [[
                InlineKeyboardButton("↩️", callback_data=f"reopen_task|{task_id}"),
                InlineKeyboardButton("⏳", callback_data=f"postpone_task|{task_id}"),
                InlineKeyboardButton("🗑️", callback_data=f"del_task|{task_id}"),
            ]]
            display_text = f"✅ {task_text}"

        sent = await context.bot.send_message(
            chat_id=chat_id,
            text=display_text,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        await _remember_task_message(context, sent)

    if view == "active":
        navigation = [
            [InlineKeyboardButton("➕ إضافة مهمة جديدة", callback_data="add_task")],
            [
                InlineKeyboardButton("✅ المهام المنجزة", callback_data="show_completed_tasks"),
                InlineKeyboardButton("⏳ المهام المؤجلة", callback_data="show_postponed_tasks"),
            ],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
        ]
    elif view == "completed":
        navigation = [
            [InlineKeyboardButton("📅 المهام اليومية", callback_data="show_tasks")],
            [InlineKeyboardButton("⏳ المهام المؤجلة", callback_data="show_postponed_tasks")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
        ]
    else:
        navigation = [
            [InlineKeyboardButton("📅 المهام اليومية", callback_data="show_tasks")],
            [InlineKeyboardButton("✅ المهام المنجزة", callback_data="show_completed_tasks")],
            [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
        ]

    navigation_message = await context.bot.send_message(
        chat_id=chat_id,
        text="اختر إجراءً من الأزرار أدناه:",
        reply_markup=InlineKeyboardMarkup(navigation),
    )
    await _remember_task_message(context, navigation_message)


# ───────────────────────────────────────────────
# معالج النصوص (الحالات + الذكاء الاصطناعي)
# ───────────────────────────────────────────────
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    user_id = user.id

    if check_spam(user_id):
        await update.message.reply_text(
            "⚠️ أرجو التمهل قليلاً، أنت تقوم بإرسال الرسائل بسرعة كبيرة!"
        )
        return

    text = update.message.text.strip()
    state = context.user_data.get("state")
    temp_data = context.user_data.get("temp_data", "")

    # ── حالة: إضافة مادة ──
    if state == "waiting_for_subject":
        sub_name = text
        try:
            check = await sb_execute(
                lambda: supabase.table("subjects")
                .select("*")
                .eq("user_id", user_id)
                .eq("subject_name", sub_name)
                .execute()
            )
        except Exception:
            logger.exception("Exception caught in handle_text()")
            check = type("obj", (object,), {"data": []})()

        if check.data:
            await update.message.reply_text(f"⚠️ المادة '{sub_name}' موجودة مسبقاً!")
        else:
            try:
                await sb_execute(
                    lambda: supabase.table("subjects").insert(
                        {"user_id": user_id, "subject_name": sub_name}
                    ).execute()
                )
            except Exception:
                logger.exception("Exception caught in handle_text()")
                pass
            clear_user_cache(user_id)
            await update.message.reply_text(
                f"✅ تمت إضافة المادة '{sub_name}' بنجاح!"
            )
        context.user_data.pop("state", None)
        context.user_data.pop("temp_data", None)
        await show_subjects_menu(update, context)
        return

    # ── حالة: إضافة فرع ──
    if state == "waiting_for_section":
        subject = temp_data
        sec_name = text
        try:
            check = await sb_execute(
                lambda: supabase.table("sections")
                .select("*")
                .eq("user_id", user_id)
                .eq("subject_name", subject)
                .eq("section_name", sec_name)
                .execute()
            )
        except Exception:
            logger.exception("Exception caught in handle_text()")
            check = type("obj", (object,), {"data": []})()

        if check.data:
            await update.message.reply_text(
                f"⚠️ الفرع '{sec_name}' موجود مسبقاً في هذه المادة!"
            )
        else:
            try:
                await sb_execute(
                    lambda: supabase.table("sections").insert({
                        "user_id": user_id,
                        "subject_name": subject,
                        "section_name": sec_name,
                    }).execute()
                )
            except Exception:
                logger.exception("Exception caught in handle_text()")
                pass
            clear_user_cache(user_id)
            await update.message.reply_text(
                f"✅ تم إضافة الفرع '{sec_name}' لمادة [{subject}] بنجاح!"
            )
        context.user_data.pop("state", None)
        context.user_data.pop("temp_data", None)
        await show_sections_menu_direct(update, context, user_id, subject)
        return

    # ── حالة: عنوان الدرس ──
    if state and state.startswith("waiting_for_lesson_title_"):
        parts = temp_data.split("|")
        subject, section = parts[0], parts[1]
        title = text
        context.user_data["state"] = f"waiting_for_lesson_content_{subject}|{section}|{title}"
        context.user_data["temp_data"] = title
        await update.message.reply_text(
            f"✍️ أرسل الآن محتوى الدرس '{title}' (أو أرسل صورة/فيديو/ملف مباشرة):"
        )
        return

    # ── حالة: محتوى الدرس نصي ──
    if state and state.startswith("waiting_for_lesson_content_"):
        parts = state.replace("waiting_for_lesson_content_", "").split("|", 2)
        subject, section, title = parts[0], parts[1], parts[2]
        lesson_content = text.strip()
        content_type = "text"
        candidate_url = lesson_content
        if candidate_url.startswith("www."):
            candidate_url = "https://" + candidate_url
        parsed_url = urlparse(candidate_url)
        if (
            parsed_url.scheme in ("http", "https")
            and parsed_url.netloc
            and not any(char.isspace() for char in candidate_url)
        ):
            content_type = "link"
            lesson_content = candidate_url
        try:
            await sb_execute(
                lambda: supabase.table("lessons").insert({
                    "user_id": user_id,
                    "subject_name": subject,
                    "section_name": section,
                    "title": title,
                    "content_type": content_type,
                    "file_or_text": lesson_content,
                }).execute()
            )
        except Exception:
            logger.exception("Exception caught in handle_text()")
            pass
        context.user_data.pop("state", None)
        context.user_data.pop("temp_data", None)
        clear_user_cache(user_id)
        await update.message.reply_text(
            f"✅ تم حفظ الدرس '{title}' في [{subject} ➔ {section}] بنجاح!"
        )
        await show_lessons_menu_direct(update, context, user_id, subject, section)
        return

    # ── حالة: تعديل اسم الدرس ──
    if state and state.startswith("waiting_for_edit_lesson_"):
        lesson_id = int(state.replace("waiting_for_edit_lesson_", ""))
        new_title = text
        try:
            await sb_execute(
                lambda: supabase.table("lessons")
                .update({"title": new_title})
                .eq("id", lesson_id)
                .eq("user_id", user_id)
                .execute()
            )
            res_lesson = await sb_execute(
                lambda: supabase.table("lessons")
                .select("subject_name, section_name")
                .eq("id", lesson_id)
                .execute()
            )
        except Exception:
            logger.exception("Exception caught in handle_text()")
            res_lesson = type("obj", (object,), {"data": []})()

        context.user_data.pop("state", None)
        context.user_data.pop("temp_data", None)
        clear_user_cache(user_id)

        if res_lesson.data:
            sub = res_lesson.data[0].get("subject_name")
            sec = res_lesson.data[0].get("section_name")
            await update.message.reply_text(
                f"✅ تم تعديل اسم الدرس بنجاح إلى: '{new_title}'"
            )
            await show_lessons_menu_direct(update, context, user_id, sub, sec)
        else:
            await update.message.reply_text("✅ تم التعديل بنجاح.")
            await show_subjects_menu(update, context)
        return

    # ── حالة: إضافة ملاحظة للمادة ──
    if state and state.startswith("waiting_for_sub_note_"):
        subject = state.replace("waiting_for_sub_note_", "")
        try:
            await sb_execute(
                lambda: supabase.table("subject_notes").insert({
                    "user_id": user_id,
                    "subject_name": subject,
                    "note_text": text,
                }).execute()
            )
        except Exception:
            logger.exception("Exception caught in handle_text()")
            pass
        context.user_data.pop("state", None)
        context.user_data.pop("temp_data", None)
        clear_user_cache(user_id)
        await update.message.reply_text(
            f"✅ تم حفظ الملاحظة لمادة [{subject}] بنجاح!"
        )
        await show_subject_notes_menu(update, context, user_id, subject)
        return

    # ── حالة: إضافة مهمة ──
    if state == "waiting_for_task":
        try:
            await sb_execute(
                lambda: supabase.table("tasks").insert(
                    {
                        "user_id": user_id,
                        "task_text": text,
                        "is_completed": False,
                        "is_postponed": False,
                    }
                ).execute()
            )
        except Exception:
            logger.exception("Exception caught in handle_text()")
            pass
        context.user_data.pop("state", None)
        context.user_data.pop("temp_data", None)
        await update.message.reply_text("✅ تم إضافة المهمة بنجاح!")
        await show_tasks_menu(update, context)
        return

    # ── كلمات مفتاحية سريعة ──
    list_keywords = ["قائمة", "اعرض", "اضهر", "أضهر", "دروسي", "الدروس", "موادي"]
    if any(keyword in text for keyword in list_keywords):
        await show_subjects_menu(update, context)
        return

    # ── fallback: الذكاء الاصطناعي ──
    await handle_ai_chat(update, context)


# ───────────────────────────────────────────────
# معالج الوسائط (صور، ملفات، فيديو)
# ───────────────────────────────────────────────
async def handle_photo_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    user_id = user.id

    if check_spam(user_id):
        return

    state = context.user_data.get("state")
    temp_data = context.user_data.get("temp_data", "")

    message = update.message
    if message.photo:
        file_id, c_type = message.photo[-1].file_id, "photo"
    elif message.document:
        file_id, c_type = message.document.file_id, "document"
    elif message.video:
        file_id, c_type = message.video.file_id, "video"
    elif message.audio:
        file_id, c_type = message.audio.file_id, "audio"
    elif message.voice:
        file_id, c_type = message.voice.file_id, "voice"
    elif message.video_note:
        file_id, c_type = message.video_note.file_id, "video_note"
    elif message.animation:
        file_id, c_type = message.animation.file_id, "animation"
    elif message.sticker:
        file_id, c_type = message.sticker.file_id, "sticker"
    else:
        return

    # ── إضافة درس بوسائط (من حالة العنوان أو المحتوى) ──
    if state and state.startswith("waiting_for_lesson_title_"):
        parts = temp_data.split("|")
        subject, section = parts[0], parts[1]
        title = update.message.caption if update.message.caption else "ملف/فيديو/صورة درس"
        try:
            await sb_execute(
                lambda: supabase.table("lessons").insert({
                    "user_id": user_id,
                    "subject_name": subject,
                    "section_name": section,
                    "title": title,
                    "content_type": c_type,
                    "file_or_text": file_id,
                }).execute()
            )
        except Exception:
            logger.exception("Exception caught in handle_photo_document()")
            pass
        context.user_data.pop("state", None)
        context.user_data.pop("temp_data", None)
        clear_user_cache(user_id)
        await update.message.reply_text(
            f"✅ تم حفظ الوسائط للدرس '{title}' في [{subject} ➔ {section}] بنجاح!"
        )
        await show_lessons_menu_direct(update, context, user_id, subject, section)
        return

    if state and state.startswith("waiting_for_lesson_content_"):
        parts = state.replace("waiting_for_lesson_content_", "").split("|", 2)
        subject, section, title = parts[0], parts[1], parts[2]
        try:
            await sb_execute(
                lambda: supabase.table("lessons").insert({
                    "user_id": user_id,
                    "subject_name": subject,
                    "section_name": section,
                    "title": title,
                    "content_type": c_type,
                    "file_or_text": file_id,
                }).execute()
            )
        except Exception:
            logger.exception("Exception caught in handle_photo_document()")
            pass
        context.user_data.pop("state", None)
        context.user_data.pop("temp_data", None)
        clear_user_cache(user_id)
        await update.message.reply_text(
            f"✅ تم حفظ الوسائط للدرس '{title}' في [{subject} ➔ {section}] بنجاح!"
        )
        await show_lessons_menu_direct(update, context, user_id, subject, section)
        return

    await update.message.reply_text(
        "⚠️ افتح «إضافة درس» أولًا، ثم أرسل محتوى الدرس: نصًا أو رابطًا أو مرفقًا مدعومًا."
    )


# ───────────────────────────────────────────────
def request_openrouter(user_message, OPENROUTER_API_KEY):
    response = requests.post(
        url="https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "HTTP-Referer": "https://telegram.org",
            "X-Title": "Telegram Study Bot"
        },
        json={
            "model": "openrouter/free",
            "messages": [
                {
                    "role": "system", 
                    "content": (
                        "أنت مساعد دراسي داخل بوت Telegram اسمه OTA / Bac2027. أجب عن أسئلة المستخدم الدراسية بوضوح وبالعربية ما لم يطلب لغة أخرى.\n"
                        "مهم جدًا: لا تذكر إلا وظائف البوت الموجودة فعلًا، ولا تخترع أوامر أو ميزات ولا تدّعِ أن أمرًا ما يعمل لمجرد أنه يبدو مفيدًا.\n"
                        "الوظائف الموجودة فعلًا: /start لفتح القائمة الرئيسية، /lessons لعرض المواد والدروس، /tasks لعرض المهام اليومية، و/help لعرض المساعدة. عبر الأزرار التفاعلية يمكن للمستخدم إضافة المواد والفروع والدروس النصية أو الوسائط، إضافة ملاحظات للمواد، وعرض هذه العناصر وتعديل عنوان الدرس وحذف العناصر، وكذلك إضافة المهام وتغيير حالتها وحذفها. يمكن للمستخدم إرسال سؤال دراسي عادي إلى خدمة الذكاء الاصطناعي إذا كانت الخدمة مفعّلة.\n"
                        "لا تقل إن البوت يدعم أوامر مثل /add_subject أو /add_branch أو /reorder_subjects أو /list_subjects أو /mark_review أو /summarize أو /mindmap أو /quiz أو /add_task أو /today أو /repeat_task أو /progress_report أو /done_task؛ هذه الأوامر غير موجودة في الكود الحالي.\n"
                        "لا تعد المستخدم بتذكيرات مجدولة أو مراجعة بالتكرار المتباعد أو تقارير أسبوعية أو جدول تلقائي أو ترتيب المواد بالسحب والإفلات أو تحديد تواريخ وأولويات للمهام؛ هذه الميزات غير مطبقة حاليًا.\n"
                        "إذا سأل المستخدم عن أمر أو ميزة غير متوفرة، قل بصراحة إنها غير متاحة حاليًا، ولا تخترع طريقة لاستخدامها، ثم وجّهه إلى المطور @ota_m_pro ليقترح الميزة المطلوبة. ولشرح طريقة استخدام البوت، وجّهه إلى الأمر /guide. لا تقل إنك حفظت مادة أو درسًا أو مهمة؛ الحفظ يتم فقط عندما ينفذ المستخدم الإجراء المناسب داخل البوت وتؤكده واجهة البوت.\n"
                        "اجعل الإجابة مرتبة ومختصرة، واستخدم نقاطًا وإيموجيات عند الحاجة، وتجنب الفقرات الطويلة."
                    )
                },
                {
                    "role": "user", 
                    "content": user_message
                }
            ]
        },
        timeout=30
    )
    return response.json()


async def handle_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OPENROUTER_API_KEY:
        await update.message.reply_text("⛔ خدمة الذكاء الاصطناعي غير متاحة حالياً.")
        return

    user_message = update.message.text
    if not user_message:
        return
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    
    try:
        # request_openrouter يستخدم requests بشكل متزامن، لذلك نشغّله في Thread
        # حتى لا يوقف Event Loop الخاص ببوت Telegram أثناء انتظار OpenRouter.
        response_data = await asyncio.to_thread(
            request_openrouter,
            user_message,
            OPENROUTER_API_KEY,
        )

        # التحقق من استجابة OpenRouter قبل الوصول إلى choices لتجنب أخطاء غير واضحة.
        if not isinstance(response_data, dict) or not response_data.get("choices"):
            raise RuntimeError("Invalid AI response from OpenRouter")

        ai_reply = response_data["choices"][0]["message"]["content"]
        await update.message.reply_text(ai_reply)
    except Exception as e:
        logger.exception("Error in AI chat")
        await update.message.reply_text("عذراً، حدث خطأ أثناء الاتصال بالذكاء الاصطناعي.")

# ───────────────────────────────────────────────
# معالج الأزرار (Callbacks)
# ───────────────────────────────────────────────
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if not user:
        return
    user_id = user.id

    if check_spam(user_id):
        await query.answer("⚠️ يرجى عدم الضغط بسرعة كبيرة!", show_alert=True)
        return

    await query.answer()
    data = query.data

    # ── القائمة الرئيسية ──
    if data == "show_guide":
        await guide_command(update, context)
        return
    elif data == "show_subjects":
        await show_subjects_menu(update, context)
        return
    elif data == "main_menu":
        context.user_data.pop("state", None)
        context.user_data.pop("temp_data", None)
        await start(update, context)
        return
    elif data == "add_subject":
        context.user_data["state"] = "waiting_for_subject"
        context.user_data["temp_data"] = ""
        await query.message.edit_text(
            "✍️ أرسل الآن اسم المادة الجديدة التي تريد إضافتها:"
        )
        return

    # ── خيارات المادة ──
    if data.startswith("sub|"):
        subject = data.split("|", 1)[1]
        await show_subject_options(update, context, user_id, subject)
        return

    if data.startswith("show_sec_list|"):
        subject = data.split("|", 1)[1]
        await show_sections_menu_direct(update, context, user_id, subject)
        return

    if data.startswith("show_sub_notes|"):
        subject = data.split("|", 1)[1]
        await show_subject_notes_menu(update, context, user_id, subject)
        return

    if data.startswith("add_sub_note|"):
        subject = data.split("|", 1)[1]
        context.user_data["state"] = f"waiting_for_sub_note_{subject}"
        context.user_data["temp_data"] = subject
        await query.message.edit_text(
            f"✍️ أرسل الآن نص الملاحظة التي تريد إضافتها لمادة [{subject}]:"
        )
        return

    if data.startswith("view_sub_note|"):
        n_id = int(data.split("|")[1])
        try:
            res = await sb_execute(
                lambda: supabase.table("subject_notes")
                .select("note_text, subject_name")
                .eq("id", n_id)
                .eq("user_id", user_id)
                .execute()
            )
        except Exception:
            logger.exception("Exception caught in button_callback()")
            res = type("obj", (object,), {"data": []})()
        if res.data:
            note_text = res.data[0].get("note_text")
            sub = res.data[0].get("subject_name")
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🔙 رجوع لملاحظات المادة", callback_data=f"show_sub_notes|{sub}"
                )
            ]])
            await query.message.edit_text(
                f"📝 **ملاحظة [{sub}]:**\n\n{note_text}",
                reply_markup=kb,
                parse_mode="Markdown",
            )
        return

    if data.startswith("del_sub_note|"):
        parts = data.split("|")
        n_id = int(parts[1])
        subject = parts[2]
        try:
            await sb_execute(
                lambda: supabase.table("subject_notes")
                .delete()
                .eq("id", n_id)
                .eq("user_id", user_id)
                .execute()
            )
        except Exception:
            logger.exception("Exception caught in button_callback()")
            pass
        clear_user_cache(user_id)
        await show_subject_notes_menu(update, context, user_id, subject)
        return

    # ── المهام ──
    if data == "show_tasks":
        await show_tasks_menu(update, context, view="active")
        return
    elif data == "show_completed_tasks":
        await show_tasks_menu(update, context, view="completed")
        return
    elif data == "show_postponed_tasks":
        await show_tasks_menu(update, context, view="postponed")
        return
    elif data == "add_task":
        context.user_data["state"] = "waiting_for_task"
        context.user_data["temp_data"] = ""
        await query.message.edit_text("✍️ أرسل الآن اسم أو نص المهمة الجديدة:")
        return
    elif data.startswith(("complete_task|", "postpone_task|", "restore_task|", "reopen_task|", "del_task|")):
        action, task_id_text = data.split("|", 1)
        try:
            task_id = int(task_id_text)
            if action == "del_task":
                result = await sb_execute(
                    lambda: supabase.table("tasks")
                    .delete()
                    .eq("id", task_id)
                    .eq("user_id", user_id)
                    .select("id")
                    .execute()
                )
                success_text = "🗑️ تم حذف المهمة."
            else:
                changes = {
                    "complete_task": {"is_completed": True, "is_postponed": False},
                    "postpone_task": {"is_completed": False, "is_postponed": True},
                    "restore_task": {"is_completed": False, "is_postponed": False},
                    "reopen_task": {"is_completed": False, "is_postponed": False},
                }[action]
                result = await sb_execute(
                    lambda: supabase.table("tasks")
                    .update(changes)
                    .eq("id", task_id)
                    .eq("user_id", user_id)
                    .select("id")
                    .execute()
                )
                success_text = {
                    "complete_task": "✅ تم إنجاز المهمة وحفظها في قائمة المنجزة.",
                    "postpone_task": "⏳ تم تأجيل المهمة وحفظها في قائمة المؤجلة.",
                    "restore_task": "↩️ تمت إعادة المهمة إلى المهام اليومية.",
                    "reopen_task": "↩️ تمت إعادة المهمة إلى المهام اليومية.",
                }[action]

            if not result.data:
                await context.bot.send_message(
                    chat_id=user_id,
                    text="⚠️ لم يتم العثور على المهمة أو لم تُحفظ العملية. حاول تحديث القائمة.",
                )
                return

            current_text = query.message.text or "المهمة"
            for prefix in ("⏳ ", "✅ ", "📝 "):
                if current_text.startswith(prefix):
                    current_text = current_text[len(prefix):]
                    break
            if action == "del_task":
                final_text = f"{success_text}\n{current_text}"
            else:
                final_text = f"{success_text}\n{current_text}"
            await query.message.edit_text(final_text, reply_markup=None)
        except Exception:
            logger.exception("Could not apply task action (callback=%s, user_id=%s)", data, user_id)
            await context.bot.send_message(
                chat_id=user_id,
                text="⚠️ تعذر حفظ التغيير في قاعدة البيانات. حاول لاحقًا.",
            )
        return
    elif data.startswith("toggle_task|"):
        # توافق مع أزرار المهام القديمة التي قد تبقى في محادثة المستخدم.
        task_id = int(data.split("|", 1)[1])
        try:
            result = await sb_execute(
                lambda: supabase.table("tasks")
                .select("is_completed")
                .eq("id", task_id)
                .eq("user_id", user_id)
                .execute()
            )
            if result.data:
                new_status = not bool(result.data[0].get("is_completed"))
                await sb_execute(
                    lambda: supabase.table("tasks")
                    .update({"is_completed": new_status, "is_postponed": False})
                    .eq("id", task_id)
                    .eq("user_id", user_id)
                    .execute()
                )
                await show_tasks_menu(update, context, view="active")
        except Exception:
            logger.exception("Legacy task toggle failed")
            await context.bot.send_message(
                chat_id=user_id,
                text="⚠️ تعذر تحديث المهمة. حاول لاحقًا.",
            )
        return
    # ── حذف مادة ──
    if data.startswith("delsub|"):
        sub_to_del = data.split("|", 1)[1]
        try:
            await sb_execute(
                lambda: supabase.table("subjects")
                .delete()
                .eq("user_id", user_id)
                .eq("subject_name", sub_to_del)
                .execute()
            )
            await sb_execute(
                lambda: supabase.table("sections")
                .delete()
                .eq("user_id", user_id)
                .eq("subject_name", sub_to_del)
                .execute()
            )
            await sb_execute(
                lambda: supabase.table("lessons")
                .delete()
                .eq("user_id", user_id)
                .eq("subject_name", sub_to_del)
                .execute()
            )
            await sb_execute(
                lambda: supabase.table("subject_notes")
                .delete()
                .eq("user_id", user_id)
                .eq("subject_name", sub_to_del)
                .execute()
            )
        except Exception:
            logger.exception("Exception caught in button_callback()")
            pass
        clear_user_cache(user_id)
        await show_subjects_menu(update, context)
        return

    # ── حذف فرع ──
    if data.startswith("delsec|"):
        parts = data.split("|")
        subject = parts[1]
        sec_to_del = parts[2]
        try:
            await sb_execute(
                lambda: supabase.table("sections")
                .delete()
                .eq("user_id", user_id)
                .eq("subject_name", subject)
                .eq("section_name", sec_to_del)
                .execute()
            )
            await sb_execute(
                lambda: supabase.table("lessons")
                .delete()
                .eq("user_id", user_id)
                .eq("subject_name", subject)
                .eq("section_name", sec_to_del)
                .execute()
            )
        except Exception:
            logger.exception("Exception caught in button_callback()")
            pass
        clear_user_cache(user_id)
        await show_sections_menu_direct(update, context, user_id, subject)
        return

    # ── فتح فرع ──
    if data.startswith("sec|"):
        parts = data.split("|")
        subject = parts[1]
        section = parts[2]
        await show_lessons_menu_direct(update, context, user_id, subject, section)
        return

    # ── إضافة فرع ──
    if data.startswith("addsec|"):
        subject = data.split("|", 1)[1]
        context.user_data["state"] = "waiting_for_section"
        context.user_data["temp_data"] = subject
        await query.message.edit_text(
            f"✍️ أرسل الآن اسم الفرع الجديد الذي تريد إضافته لمادة [{subject}]:"
        )
        return

    # ── إضافة درس ──
    if data.startswith("addles|"):
        parts = data.split("|")
        subject = parts[1]
        section = parts[2]
        context.user_data["state"] = f"waiting_for_lesson_title_{subject}|{section}"
        context.user_data["temp_data"] = f"{subject}|{section}"
        await query.message.edit_text(
            f"✍️ أرسل الآن **عنوان الدرس** الجديد في [{subject} ➔ {section}]:",
            parse_mode="Markdown",
        )
        return

    # ── فتح درس ──
    if data.startswith("op|"):
        lesson_id = int(data.split("|")[1])
        try:
            res = await sb_execute(
                lambda: supabase.table("lessons")
                .select("title, content_type, file_or_text, subject_name, section_name")
                .eq("id", lesson_id)
                .eq("user_id", user_id)
                .execute()
            )
        except Exception:
            logger.exception("Exception caught in button_callback()")
            res = type("obj", (object,), {"data": []})()
        if res.data:
            lesson = res.data[0]
            title = lesson.get("title")
            c_type = lesson.get("content_type")
            content = lesson.get("file_or_text")
            subject = lesson.get("subject_name")
            section = lesson.get("section_name")

            keyboard_rows = []
            if c_type == "link" and content:
                keyboard_rows.append([
                    InlineKeyboardButton("🔗 فتح الرابط", url=content)
                ])
            keyboard_rows.extend([
                [InlineKeyboardButton("✏️ تعديل اسم الدرس", callback_data=f"edit_lesson|{lesson_id}")],
                [InlineKeyboardButton("🔙 رجوع للدروس", callback_data=f"sec|{subject}|{section}")],
            ])
            kb = InlineKeyboardMarkup(keyboard_rows)

            if c_type in ("text", "link"):
                display_content = f"📖 الدرس: {title}\n\n{content}"
                await query.message.edit_text(display_content, reply_markup=kb)
            elif c_type == "photo":
                await query.message.delete()
                await context.bot.send_photo(
                    chat_id=user_id, photo=content,
                    caption=f"📖 الدرس: {title}", reply_markup=kb,
                )
            elif c_type == "video":
                await query.message.delete()
                await context.bot.send_video(
                    chat_id=user_id, video=content,
                    caption=f"📖 الدرس: {title}", reply_markup=kb,
                )
            elif c_type == "document":
                await query.message.delete()
                await context.bot.send_document(
                    chat_id=user_id, document=content,
                    caption=f"📖 الدرس: {title}", reply_markup=kb,
                )
            elif c_type == "audio":
                await query.message.delete()
                await context.bot.send_audio(
                    chat_id=user_id, audio=content,
                    caption=f"📖 الدرس: {title}", reply_markup=kb,
                )
            elif c_type == "voice":
                await query.message.delete()
                await context.bot.send_voice(
                    chat_id=user_id, voice=content,
                    caption=f"📖 الدرس: {title}", reply_markup=kb,
                )
            elif c_type == "animation":
                await query.message.delete()
                await context.bot.send_animation(
                    chat_id=user_id, animation=content,
                    caption=f"📖 الدرس: {title}", reply_markup=kb,
                )
            elif c_type in ("video_note", "sticker"):
                await query.message.delete()
                if c_type == "video_note":
                    await context.bot.send_video_note(chat_id=user_id, video_note=content)
                else:
                    await context.bot.send_sticker(chat_id=user_id, sticker=content)
                await context.bot.send_message(
                    chat_id=user_id,
                    text=f"📖 الدرس: {title}",
                    reply_markup=kb,
                )
        return

    # ── حذف درس ──
    if data.startswith("dl|"):
        lesson_id = int(data.split("|")[1])
        try:
            res = await sb_execute(
                lambda: supabase.table("lessons")
                .select("subject_name, section_name")
                .eq("id", lesson_id)
                .eq("user_id", user_id)
                .execute()
            )
        except Exception:
            logger.exception("Exception caught in button_callback()")
            res = type("obj", (object,), {"data": []})()
        sub = sec = None
        if res.data:
            sub = res.data[0].get("subject_name")
            sec = res.data[0].get("section_name")
        try:
            await sb_execute(
                lambda: supabase.table("lessons")
                .delete()
                .eq("id", lesson_id)
                .eq("user_id", user_id)
                .execute()
            )
        except Exception:
            logger.exception("Exception caught in button_callback()")
            pass
        clear_user_cache(user_id)
        if sub and sec:
            await show_lessons_menu_direct(update, context, user_id, sub, sec)
        else:
            await show_subjects_menu(update, context)
        return

    # ── تعديل درس ──
    if data.startswith("edit_lesson|"):
        lesson_id = int(data.split("|")[1])
        context.user_data["state"] = f"waiting_for_edit_lesson_{lesson_id}"
        context.user_data["temp_data"] = str(lesson_id)
        await query.message.edit_text(
            "✍️ أرسل الآن الاسم أو العنوان الجديد للدرس:"
        )
        return


# ───────────────────────────────────────────────
# الدالة الرئيسية
# ───────────────────────────────────────────────
def main():
    # تشغيل Flask في thread منفصل (للـ Health Check)
    keep_alive()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(
            HTTPXRequest(
                connect_timeout=10,
                read_timeout=30,
                write_timeout=30,
                pool_timeout=10,
            )
        )
        .post_init(post_init)
        .build()
    )

    # الأوامر
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("guide", guide_command))
    application.add_handler(CommandHandler("lessons", lessons_command))
    application.add_handler(CommandHandler("tasks", tasks_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("users", list_all_users))
    application.add_handler(CommandHandler("broadcast", broadcast_message))

    # الأزرار التفاعلية
    application.add_handler(CallbackQueryHandler(button_callback))

    # النصوص: يفحص حالات الإضافة أولاً، ثم يسقط للذكاء الاصطناعي
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    # الوسائط
    application.add_handler(
        MessageHandler(
            filters.PHOTO
            | filters.Document.ALL
            | filters.VIDEO
            | filters.AUDIO
            | filters.VOICE
            | filters.VIDEO_NOTE
            | filters.ANIMATION
            | filters.Sticker.ALL,
            handle_photo_document,
        )
    )

    # معالج الأخطاء
    application.add_error_handler(error_handler)

    logger.info("Bot is running...")
    application.run_polling()


if __name__ == "__main__":
    main()
