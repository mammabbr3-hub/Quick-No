from app.services.admin import setting
async def admin_log(bot, session, text):
    chat=await setting(session,'admin_log_group_id','')
    if not chat: return False
    try:
        await bot.send_message(int(chat), text, parse_mode='HTML')
        return True
    except Exception:
        return False
