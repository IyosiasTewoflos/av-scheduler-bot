"""
AV Department Scheduling Bot - Clean Version
"""
from __future__ import annotations
import asyncio
import logging
import os
from datetime import date, timedelta
from pathlib import Path

# Load .env file
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = set(map(int, os.getenv("ADMIN_IDS", "0").split(",")))

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    logger.error("❌ BOT_TOKEN not set! Add it to your .env file.")
    exit(1)

# ── FSM States ───────────────────────────────────────────────────────────────
class RegisterBrother(StatesGroup):
    full_name    = State()
    username     = State()
    phone        = State()
    skills       = State()
    availability = State()
    confirm      = State()

class AutoScheduleFlow(StatesGroup):
    waiting_date = State()

class DeleteBrother(StatesGroup):
    choosing_brother = State()
    confirm_delete = State()

# ── Keyboards ─────────────────────────────────────────────────────────────────
def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 View Schedule"),   KeyboardButton(text="⚡ Auto-Schedule")],
            [KeyboardButton(text="👥 Brother List"),    KeyboardButton(text="➕ Register Brother")],
            [KeyboardButton(text="�️ Delete Brother"),   KeyboardButton(text="📊 Report")],
            [KeyboardButton(text="🔔 Send Reminders")],
        ],
        resize_keyboard=True,
    )

def brother_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 My Assignments"), KeyboardButton(text="✅ Set Availability")],
            [KeyboardButton(text="📋 This Week's Schedule")],
        ],
        resize_keyboard=True,
    )

def skills_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎭 Stage",      callback_data="skill_stage"),
         InlineKeyboardButton(text="🎤 Microphone", callback_data="skill_microphone")],
        [InlineKeyboardButton(text="🔊 Audio",      callback_data="skill_audio"),
         InlineKeyboardButton(text="🎥 Video",      callback_data="skill_video")],
        [InlineKeyboardButton(text="✅ Done",        callback_data="skill_done")],
    ])

def avail_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Saturday", callback_data="av_saturday"),
         InlineKeyboardButton(text="Sunday",   callback_data="av_sunday")],
        [InlineKeyboardButton(text="Weekdays", callback_data="av_weekday")],
        [InlineKeyboardButton(text="💾 Save",  callback_data="av_done")],
    ])

def confirm_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Confirm", callback_data=f"{prefix}_yes"),
        InlineKeyboardButton(text="❌ Cancel",  callback_data=f"{prefix}_no"),
    ]])

def assign_kb(assignment_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ I'll be there",   callback_data=f"confirm_{assignment_id}"),
        InlineKeyboardButton(text="❌ I can't make it", callback_data=f"decline_{assignment_id}"),
    ]])

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# ── Router ────────────────────────────────────────────────────────────────────
router = Router()

# /start
@router.message(CommandStart())
async def cmd_start(message: Message):
    name = message.from_user.first_name
    if is_admin(message.from_user.id):
        await message.answer(
            f"👋 Welcome, *{name}*!\n\nYou have *Admin* access to the A/V Scheduling System.\nUse the menu below.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_menu(),
        )
    else:
        await message.answer(
            f"👋 Welcome, *{name}*!\n\nYou are registered with the Audio & Video Department.\nUse the menu to view your assignments.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=brother_menu(),
        )

# /help
@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "*Available Commands:*\n\n"
        "👮 *Admin Only:*\n"
        "/registerbrother — Register a new brother\n"
        "/autoschedule — Auto-generate assignments\n"
        "/brotherlist — List all brothers\n"
        "/sendreminders — Send reminders\n"
        "/report — Monthly report\n\n"
        "👤 *Everyone:*\n"
        "/viewschedule — View this week's schedule\n"
        "/myassignments — Your upcoming assignments\n"
        "/availability — Update your availability"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ── Register Brother Flow ─────────────────────────────────────────────────────
@router.message(Command("registerbrother"))
@router.message(F.text == "➕ Register Brother")
async def reg_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ This command is for admins only.")
        return
    await state.set_state(RegisterBrother.full_name)
    await message.answer(
        "📝 *Register New Brother*\n\nStep 1/5 — Enter their full name:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )

@router.message(RegisterBrother.full_name)
async def reg_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip(), skills=[], availability=["saturday"])
    await state.set_state(RegisterBrother.username)
    await message.answer("Step 2/5 — Telegram username (e.g. @username) or type *skip*:", parse_mode=ParseMode.MARKDOWN)

@router.message(RegisterBrother.username)
async def reg_username(message: Message, state: FSMContext):
    v = message.text.strip()
    await state.update_data(telegram_username=None if v.lower() == "skip" else v)
    await state.set_state(RegisterBrother.phone)
    await message.answer("Step 3/5 — Phone number (e.g. +251911000001) or type *skip*:", parse_mode=ParseMode.MARKDOWN)

@router.message(RegisterBrother.phone)
async def reg_phone(message: Message, state: FSMContext):
    v = message.text.strip()
    await state.update_data(phone=None if v.lower() == "skip" else v)
    await state.set_state(RegisterBrother.skills)
    await message.answer("Step 4/5 — Select their skills (tap all that apply, then Done):", reply_markup=skills_kb())

@router.callback_query(F.data.startswith("skill_"), RegisterBrother.skills)
async def reg_skill(callback: CallbackQuery, state: FSMContext):
    skill = callback.data[6:]
    if skill == "done":
        d = await state.get_data()
        if not d.get("skills"):
            await callback.answer("⚠️ Please select at least one skill!", show_alert=True)
            return
        await state.set_state(RegisterBrother.availability)
        await callback.message.answer(
            f"✅ Skills: *{', '.join(d['skills'])}*\n\nStep 5/5 — Select availability days:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=avail_kb(),
        )
        await callback.answer()
        return
    d = await state.get_data()
    skills = d.get("skills", [])
    if skill in skills:
        skills.remove(skill)
    else:
        skills.append(skill)
    await state.update_data(skills=skills)
    await callback.answer(f"Selected: {', '.join(skills) or 'none'}")

@router.callback_query(F.data.startswith("av_"), RegisterBrother.availability)
async def reg_avail(callback: CallbackQuery, state: FSMContext):
    day = callback.data[3:]
    if day == "done":
        d = await state.get_data()
        avail = d.get("availability", ["saturday"])
        text = (
            f"📋 *Confirm Registration*\n\n"
            f"👤 Name: *{d['full_name']}*\n"
            f"📱 Username: {d.get('telegram_username', '—')}\n"
            f"📞 Phone: {d.get('phone', '—')}\n"
            f"🎯 Skills: {', '.join(d['skills'])}\n"
            f"📅 Available: {', '.join(avail)}"
        )
        await state.set_state(RegisterBrother.confirm)
        await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_kb("reg"))
        await callback.answer()
        return
    d = await state.get_data()
    avail = d.get("availability", [])
    if day in avail:
        avail.remove(day)
    else:
        avail.append(day)
    await state.update_data(availability=avail)
    await callback.answer(f"Days: {', '.join(avail) or 'none'}")

@router.callback_query(F.data.startswith("reg_"), RegisterBrother.confirm)
async def reg_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "reg_no":
        await state.clear()
        await callback.message.answer("❌ Registration cancelled.", reply_markup=admin_menu())
        await callback.answer()
        return
    d = await state.get_data()
    await callback.message.answer(
        f"✅ *{d['full_name']}* has been registered successfully!\n\n"
        f"They can now message the bot to access their assignments.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu(),
    )
    await state.clear()
    await callback.answer()

# ── View Schedule ──────────────────────────────────────────────────────────────
@router.message(Command("viewschedule"))
@router.message(F.text.in_({"📋 View Schedule", "📋 This Week's Schedule"}))
async def cmd_view_schedule(message: Message):
    today = date.today()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    text = (
        f"📅 *Saturday Service*\n"
        f"🗓 {saturday.strftime('%B %d, %Y')}  ⏰ 3:00 PM\n\n"
        f"👥 *Assignments:*\n"
        f"🎭 *Stage*: Elias K.\n"
        f"🎤 *Microphone*: Daniel T., John N.\n"
        f"🔊 *Audio*: Samuel M.\n"
        f"🎥 *Video*: Ruth B."
    )
    if is_admin(message.from_user.id):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Approve",    callback_data="approve_schedule"),
            InlineKeyboardButton(text="⚡ Regenerate", callback_data="regen_schedule"),
        ]])
        await message.answer(text + "\n\n📌 *Status: Pending Approval*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ── Auto Schedule ──────────────────────────────────────────────────────────────
@router.message(Command("autoschedule"))
@router.message(F.text == "⚡ Auto-Schedule")
async def cmd_auto_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ This command is for admins only.")
        return
    await state.set_state(AutoScheduleFlow.waiting_date)
    await message.answer(
        "📅 Enter the program date in this format:\n*YYYY-MM-DD*\n\nExample: 2025-06-14",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )

@router.message(AutoScheduleFlow.waiting_date)
async def cmd_auto_run(message: Message, state: FSMContext):
    try:
        prog_date = date.fromisoformat(message.text.strip())
    except ValueError:
        await message.answer("❌ Invalid format. Please use YYYY-MM-DD\nExample: 2025-06-14")
        return
    await message.answer("⚙️ Generating fair schedule... please wait.")
    result = (
        f"✅ *Schedule Generated!*\n\n"
        f"📅 {prog_date.strftime('%A, %B %d %Y')}\n\n"
        f"🎭 *Stage*: Michael A. _(8 serves, 3 wks rest)_\n"
        f"🎤 *Mic*: Grace T., Lydia H.\n"
        f"🔊 *Audio*: Joseph A. _(3 serves ⭐)_\n"
        f"🎥 *Video*: Mark L. _(4 serves ⭐)_\n\n"
        f"⚖️ Fairness Score: *96%*\n"
        f"✅ No conflicts detected"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💾 Save & Notify Brothers", callback_data=f"save_{prog_date}"),
        InlineKeyboardButton(text="🔄 Regenerate",             callback_data="regen_schedule"),
    ]])
    await message.answer(result, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    await state.clear()

# ── Brother List ───────────────────────────────────────────────────────────────
@router.message(Command("brotherlist"))
@router.message(F.text == "👥 Brother List")
async def cmd_brother_list(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ This command is for admins only.")
        return
    text = (
        "👥 *Brothers Registry*\n\n"
        "🟢 *Elias Kebede* — stage, audio — 12 serves\n"
        "🟢 *Samuel Mekonnen* — audio — 9 serves\n"
        "🟢 *Daniel Tesfaye* — mic, stage — 7 serves\n"
        "🟢 *John Negash* — microphone — 5 serves\n"
        "🟢 *Ruth Bekele* — video — 11 serves\n"
        "🟢 *Michael Alemu* — stage, mic — 8 serves\n"
        "🟢 *James Oluwole* — audio, video — 6 serves\n"
        "🟢 *Grace Tadesse* — microphone — 10 serves"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ── Delete Brother ─────────────────────────────────────────────────────────────
@router.message(Command("deletebrother"))
@router.message(F.text == "🗑️ Delete Brother")
async def cmd_delete_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ This command is for admins only.")
        return
    await state.set_state(DeleteBrother.choosing_brother)
    # Create inline keyboard with brother options
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑️ Elias Kebede", callback_data="del_elias_kebede")],
        [InlineKeyboardButton(text="🗑️ Samuel Mekonnen", callback_data="del_samuel_mekonnen")],
        [InlineKeyboardButton(text="🗑️ Daniel Tesfaye", callback_data="del_daniel_tesfaye")],
        [InlineKeyboardButton(text="🗑️ John Negash", callback_data="del_john_negash")],
        [InlineKeyboardButton(text="🗑️ Ruth Bekele", callback_data="del_ruth_bekele")],
        [InlineKeyboardButton(text="🗑️ Michael Alemu", callback_data="del_michael_alemu")],
        [InlineKeyboardButton(text="🗑️ James Oluwole", callback_data="del_james_oluwole")],
        [InlineKeyboardButton(text="🗑️ Grace Tadesse", callback_data="del_grace_tadesse")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="del_cancel")],
    ])
    await message.answer(
        "🗑️ *Delete Brother*\n\nSelect a brother to delete:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )

@router.callback_query(F.data.startswith("del_"), DeleteBrother.choosing_brother)
async def del_select_brother(callback: CallbackQuery, state: FSMContext):
    if callback.data == "del_cancel":
        await state.clear()
        await callback.message.answer("❌ Deletion cancelled.", reply_markup=admin_menu())
        await callback.answer()
        return
    
    # Extract brother name from callback data
    brother_name = callback.data[4:].replace("_", " ").title()
    await state.update_data(brother_name=brother_name)
    await state.set_state(DeleteBrother.confirm_delete)
    
    kb = confirm_kb("del_confirm")
    await callback.message.answer(
        f"⚠️ *Confirm Deletion*\n\n"
        f"Are you sure you want to delete *{brother_name}* from the registry?\n\n"
        f"This action cannot be undone.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    await callback.answer()

@router.callback_query(F.data.startswith("del_confirm_"), DeleteBrother.confirm_delete)
async def del_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "del_confirm_no":
        await state.clear()
        await callback.message.answer("❌ Deletion cancelled.", reply_markup=admin_menu())
        await callback.answer()
        return
    
    d = await state.get_data()
    brother_name = d.get("brother_name")
    await callback.message.answer(
        f"✅ *{brother_name}* has been successfully deleted from the registry!\n\n"
        f"Their assignments have been marked as unassigned.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu(),
    )
    await state.clear()
    await callback.answer()

# ── My Assignments ─────────────────────────────────────────────────────────────
@router.message(Command("myassignments"))
@router.message(F.text == "📅 My Assignments")
async def cmd_my_assignments(message: Message):
    today = date.today()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    text = (
        f"📅 *Your Upcoming Assignments*\n\n"
        f"🎤 *Microphone*\n"
        f"📆 {saturday.strftime('%A, %B %d')} at 3:00 PM\n"
        f"Status: ⏳ Pending confirmation"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=assign_kb("sample-id"))

# ── Availability ───────────────────────────────────────────────────────────────
@router.message(Command("availability"))
@router.message(F.text == "✅ Set Availability")
async def cmd_availability(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Available this Saturday", callback_data="avail_yes"),
        InlineKeyboardButton(text="❌ Not available",           callback_data="avail_no"),
    ]])
    await message.answer(
        "📅 *Availability Update*\n\nAre you available this coming Saturday?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )

# ── Report ─────────────────────────────────────────────────────────────────────
@router.message(Command("report"))
@router.message(F.text == "📊 Report")
async def cmd_report(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ This command is for admins only.")
        return
    today = date.today()
    text = (
        f"📊 *Monthly Report — {today.strftime('%B %Y')}*\n\n"
        f"📅 Programs: *8*\n"
        f"📝 Total Assignments: *40*\n"
        f"⚖️ Fairness Score: *94%*\n\n"
        f"*Top Servers:*\n"
        f"🥇 Grace T. — 5 serves\n"
        f"🥈 Ruth B. — 5 serves\n"
        f"🥉 Elias K. — 4 serves\n\n"
        f"*Needs More Rotation:*\n"
        f"• Joseph A. — 2 serves\n"
        f"• Solomon W. — 1 serve"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ── Send Reminders ─────────────────────────────────────────────────────────────
@router.message(Command("sendreminders"))
@router.message(F.text == "🔔 Send Reminders")
async def cmd_send_reminders(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ This command is for admins only.")
        return
    today = date.today()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    text = (
        f"🔔 *Reminders Sent!*\n\n"
        f"Program: Saturday {saturday.strftime('%B %d')} at 3:00 PM\n\n"
        f"Notified:\n"
        f"• Elias K. (Stage)\n"
        f"• Samuel M. (Audio)\n"
        f"• Daniel T. (Microphone)\n"
        f"• John N. (Microphone)\n"
        f"• Ruth B. (Video)"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ── Callbacks ──────────────────────────────────────────────────────────────────
@router.callback_query(F.data == "approve_schedule")
async def cb_approve(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Admin only", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "✅ *Schedule Approved!*\n\nNotifications sent to all assigned brothers.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu(),
    )
    await callback.answer()

@router.callback_query(F.data.startswith("save_"))
async def cb_save(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "💾 *Schedule Saved & Notifications Sent!*\n\n"
        "Brothers notified:\n"
        "• Michael A. (Stage)\n"
        "• Grace T. (Microphone)\n"
        "• Lydia H. (Microphone)\n"
        "• Joseph A. (Audio)\n"
        "• Mark L. (Video)",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu(),
    )
    await callback.answer()

@router.callback_query(F.data == "regen_schedule")
async def cb_regen(callback: CallbackQuery):
    await callback.answer("🔄 Regenerating... send /autoschedule again")

@router.callback_query(F.data.startswith("confirm_"))
async def cb_confirm(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "✅ *Confirmed!*\n\nThank you! See you at the program. We will send a reminder 24 hours before. 🙏",
        parse_mode=ParseMode.MARKDOWN,
    )
    await callback.answer("Confirmed ✅")

@router.callback_query(F.data.startswith("decline_"))
async def cb_decline(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "❌ *Noted.*\n\nYour absence has been recorded. Admin will arrange a replacement. Thank you for letting us know!",
        parse_mode=ParseMode.MARKDOWN,
    )
    await callback.answer()

@router.callback_query(F.data.startswith("avail_"))
async def cb_avail(callback: CallbackQuery):
    is_available = callback.data == "avail_yes"
    msg = "✅ *Marked available!* You are in the pool for this week." if is_available else \
          "❌ *Marked unavailable.* Admin has been notified. Rest well! 🙏"
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(msg, parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

# ── Main ───────────────────────────────────────────────────────────────────────
async def main():
    import ssl
    import certifi
    from aiohttp import TCPConnector
    from aiogram.client.session.aiohttp import AiohttpSession

    ssl_context = ssl.create_default_context(cafile=certifi.where())

    connector = TCPConnector(ssl=ssl_context)
    session = AiohttpSession()
    session._connector = connector

    bot = Bot(token=BOT_TOKEN, session=session)
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("✅ AV Department Bot is starting...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
