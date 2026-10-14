                    parse_mode="Markdown",
                )
            elif c_type == "document":
                await query.message.delete()
                await context.bot.send_document(
                    chat_id=user_id,
                    document=content,
                    caption=f"📖 **الدرس: {title}**",
                    reply_markup=kb,
                    parse_mode="Markdown",
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
            filters.PHOTO | filters.Document.ALL | filters.VIDEO,
            handle_photo_document,
        )
    )

    # معالج الأخطاء
    application.add_error_handler(error_handler)

    logger.info("Bot is running...")
    application.run_polling()


if __name__ == "__main__":
    main()
