from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

def main_menu():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Buy WhatsApp OTP"),KeyboardButton(text="💰 My Balance")],[KeyboardButton(text="💳 Add Funds"),KeyboardButton(text="📦 My Orders")],[KeyboardButton(text="🧾 Transactions"),KeyboardButton(text="📊 My Statistics")],[KeyboardButton(text="📢 Announcements"),KeyboardButton(text="🎧 Support")],[KeyboardButton(text="ℹ️ Help")]], resize_keyboard=True)

def join_gate(group_url: str|None, channel_url: str|None):
    rows=[]
    if group_url: rows.append([InlineKeyboardButton(text="👥 Join Group",url=group_url)])
    if channel_url: rows.append([InlineKeyboardButton(text="📢 Join Channel",url=channel_url)])
    rows.append([InlineKeyboardButton(text="✅ I’ve Joined",callback_data="join_check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
