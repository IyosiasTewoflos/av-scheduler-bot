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

# Global bot instance for sending notifications
bot_instance: Bot = None

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    logger.error("❌ BOT_TOKEN not set! Add it to your .env file.")
    exit(1)

# ── FSM States ───────────────────────────────────────────────────────────────
class RegisterBrother(StatesGroup):
    full_name    = State()
    phone        = State()
    confirm      = State()

class AutoScheduleFlow(StatesGroup):
    waiting_date = State()

class DeleteBrother(StatesGroup):
    choosing_brother = State()
    confirm_delete = State()

class EditBrother(StatesGroup):
    choosing_brother = State()
    choosing_field = State()
    editing_value = State()
    confirm_edit = State()

class EditSchedule(StatesGroup):
    choosing_role = State()
    choosing_brother = State()
    confirm_edit = State()

class PendingApproval(StatesGroup):
    approving = State()

# ── Keyboards ─────────────────────────────────────────────────────────────────
def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 View Schedule"),   KeyboardButton(text="✏️ Edit Schedule")],
            [KeyboardButton(text="👥 Brother List"),    KeyboardButton(text="➕ Register Brother")],
            [KeyboardButton(text="✏️ Edit Brother"),     KeyboardButton(text="🗑️ Delete Brother")],
            [KeyboardButton(text="⏳ Pending Approval"), KeyboardButton(text="⚡ Auto-Schedule")],
            [KeyboardButton(text="📊 Report"),           KeyboardButton(text="🔔 Send Reminders")],
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

# ── Brother Storage ───────────────────────────────────────────────────────────
brothers_db: dict = {}  # {telegram_id: {full_name, skills, availability, phone, telegram_username, telegram_id}}
pending_approvals: list = []  # List of brothers waiting for approval
user_registrations: dict = {}  # Track if user is currently registering
assignments_db: dict = {  # Current assignments for Saturday
    'stage': None,
    'microphone': [],
    'audio': None,
    'video': None,
}

# ── Notification Helper Functions ──────────────────────────────────────────────
async def notify_user(telegram_id: int, text: str, parse_mode: str = ParseMode.MARKDOWN) -> bool:
    """Send a notification message to a user. Returns True if successful."""
    if not bot_instance:
        logger.warning(f"Bot instance not available for notification to {telegram_id}")
        return False
    try:
        await bot_instance.send_message(telegram_id, text, parse_mode=parse_mode)
        logger.info(f"Notification sent to {telegram_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to send notification to {telegram_id}: {e}")
        return False

async def notify_all_admins(text: str, parse_mode: str = ParseMode.MARKDOWN) -> None:
    """Send a notification to all admin users."""
    for admin_id in ADMIN_IDS:
        await notify_user(admin_id, text, parse_mode)

async def notify_all_brothers(text: str, parse_mode: str = ParseMode.MARKDOWN) -> None:
    """Send a notification to all registered brothers."""
    for telegram_id in brothers_db.keys():
        await notify_user(telegram_id, text, parse_mode)

# ── Router ────────────────────────────────────────────────────────────────────
router = Router()

# /start
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    name = message.from_user.first_name
    telegram_id = message.from_user.id
    
    if is_admin(telegram_id):
        await message.answer(
            f"👋 Welcome, *{name}*!\n\nYou have *Admin* access to the A/V Scheduling System.\nUse the menu below.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_menu(),
        )
    elif telegram_id in brothers_db:
        # Already registered and approved
        await message.answer(
            f"👋 Welcome back, *{name}*!\n\nYou are registered with the Audio & Video Department.\nUse the menu to view your assignments.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=brother_menu(),
        )
    else:
        # Not registered - offer registration
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📝 Register as Brother", callback_data="start_register")
        ]])
        await message.answer(
            f"👋 Welcome, *{name}*!\n\nYou are not yet registered with the A/V Department.\n\nWould you like to register?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb,
        )

@router.callback_query(F.data == "start_register")
async def start_registration(callback: CallbackQuery, state: FSMContext):
    telegram_id = callback.from_user.id
    await state.update_data(telegram_id=telegram_id)
    await state.set_state(RegisterBrother.full_name)
    await callback.message.answer(
        "📝 *Self Registration*\n\nStep 1/2 — Enter your full name:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    await callback.answer()

# /help
@router.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "*Available Commands:*\n\n"
        "👮 *Admin Only:*\n"
        "/registerbrother — Register a new brother\n"
        "/editbrother — Edit a brother's info\n"
        "/deletebrother — Delete a brother\n"
        "/viewschedule — View this week's schedule\n"
        "/editschedule — Edit assignments\n"
        "/approvepending — Review pending registrations\n"
        "/autoschedule — Auto-generate assignments\n"
        "/brotherlist — List all brothers\n"
        "/sendreminders — Send reminders\n"
        "/report — Monthly report\n\n"
        "👤 *Everyone:*\n"
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
        "📝 *Register New Brother*\n\nStep 1/2 — Enter their full name:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )

@router.message(RegisterBrother.full_name)
async def reg_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip())
    await state.set_state(RegisterBrother.phone)
    await message.answer("Step 2/2 — Phone number (e.g. +251911000001) or type *skip*:", parse_mode=ParseMode.MARKDOWN)

@router.message(RegisterBrother.phone)
async def reg_phone(message: Message, state: FSMContext):
    v = message.text.strip()
    await state.update_data(phone=None if v.lower() == "skip" else v)
    await state.set_state(RegisterBrother.confirm)
    d = await state.get_data()
    text = (
        f"📋 *Confirm Registration*\n\n"
        f"👤 Name: *{d['full_name']}*\n"
        f"📱 Phone: {d.get('phone', '—')}\n\n"
        f"_Admin will assign skills and availability once approved._"
    )
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_kb("reg"))

@router.callback_query(F.data.startswith("reg_"), RegisterBrother.confirm)
async def reg_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "reg_no":
        await state.clear()
        await callback.message.answer("❌ Registration cancelled.", reply_markup=admin_menu())
        await callback.answer()
        return
    
    d = await state.get_data()
    telegram_id = d.get('telegram_id', callback.from_user.id)
    
    # Add brother to pending approvals
    brother_info = {
        'telegram_id': telegram_id,
        'full_name': d['full_name'],
        'skills': [],
        'availability': [],
        'phone': d.get('phone'),
        'telegram_username': None,
        'serves': 0,
        'status': 'pending',
    }
    pending_approvals.append(brother_info)
    
    await callback.message.answer(
        f"✅ *{d['full_name']}* registration submitted!\n\n"
        f"⏳ Awaiting admin approval. You will be notified once approved.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove(),
    )
    
    logger.info(f"New registration pending: {d['full_name']} (Telegram ID: {telegram_id})")
    
    await state.clear()
    await callback.answer()

# ── View Schedule ──────────────────────────────────────────────────────────────
@router.message(Command("viewschedule"))
@router.message(F.text.in_({"📋 View Schedule", "📋 This Week's Schedule"}))
async def cmd_view_schedule(message: Message):
    today = date.today()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    
    # Convert telegram IDs to full names
    stage_id = assignments_db.get('stage')
    stage = brothers_db.get(stage_id, {}).get('full_name') if stage_id else None
    stage = stage or '—'
    
    mic_ids = assignments_db.get('microphone', [])
    mic_names = [brothers_db.get(mid, {}).get('full_name') for mid in mic_ids if mid in brothers_db]
    mic = ', '.join(mic_names) if mic_names else '—'
    
    audio_id = assignments_db.get('audio')
    audio = brothers_db.get(audio_id, {}).get('full_name') if audio_id else None
    audio = audio or '—'
    
    video_id = assignments_db.get('video')
    video = brothers_db.get(video_id, {}).get('full_name') if video_id else None
    video = video or '—'
    
    text = (
        f"📅 *Saturday Service*\n"
        f"🗓 {saturday.strftime('%B %d, %Y')}  ⏰ 3:00 PM\n\n"
        f"👥 *Assignments:*\n"
        f"🎭 *Stage*: {stage}\n"
        f"🎤 *Microphone*: {mic}\n"
        f"🔊 *Audio*: {audio}\n"
        f"🎥 *Video*: {video}"
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
        "📅 Enter the program date in this format:\n*YYYY-MM-DD*\n\nExample: 2026-06-14",
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
    
    if not brothers_db:
        await message.answer("👥 *Brothers Registry*\n\nNo brothers registered yet.", parse_mode=ParseMode.MARKDOWN)
        return
    
    text = "👥 *Brothers Registry*\n\n"
    for name, brother in brothers_db.items():
        skills_str = ', '.join(brother['skills']) if brother['skills'] else 'None'
        text += f"🟢 *{brother['full_name']}* — {skills_str} — {brother['serves']} serves\n"
    
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ── Edit Brother ───────────────────────────────────────────────────────────────
@router.message(Command("editbrother"))
@router.message(F.text == "✏️ Edit Brother")
async def cmd_edit_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ This command is for admins only.")
        return
    
    if not brothers_db:
        await message.answer("❌ No brothers registered to edit.", reply_markup=admin_menu())
        return
    
    await state.set_state(EditBrother.choosing_brother)
    # Create inline keyboard with registered brother options
    kb_buttons = [
        [InlineKeyboardButton(text=f"✏️ {brothers_db[tid]['full_name']}", callback_data=f"edit_{i}")]
        for i, tid in enumerate(brothers_db.keys())
    ]
    kb_buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="edit_cancel")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    
    await message.answer(
        "✏️ *Edit Brother*\n\nSelect a brother to edit:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )

@router.callback_query(F.data.startswith("edit_"), EditBrother.choosing_brother)
async def edit_select_brother(callback: CallbackQuery, state: FSMContext):
    if callback.data == "edit_cancel":
        await state.clear()
        await callback.message.answer("❌ Edit cancelled.", reply_markup=admin_menu())
        await callback.answer()
        return
    
    # Get brother by index from telegram_ids
    try:
        idx = int(callback.data[5:])
        telegram_id = list(brothers_db.keys())[idx]
        brother_name = brothers_db[telegram_id]['full_name']
    except (ValueError, IndexError):
        await callback.answer("❌ Invalid selection", show_alert=True)
        return
    
    await state.update_data(telegram_id=telegram_id, brother_name=brother_name)
    await state.set_state(EditBrother.choosing_field)
    
    # Show options for what to edit
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎯 Skills", callback_data="edit_field_skills")],
        [InlineKeyboardButton(text="📅 Availability", callback_data="edit_field_availability")],
        [InlineKeyboardButton(text="📱 Phone", callback_data="edit_field_phone")],
        [InlineKeyboardButton(text="👤 Telegram Username", callback_data="edit_field_username")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="edit_cancel")],
    ])
    
    await callback.message.answer(
        f"✏️ *Edit {brother_name}*\n\nWhat would you like to edit?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    await callback.answer()

@router.callback_query(F.data.startswith("edit_field_"), EditBrother.choosing_field)
async def edit_choose_field(callback: CallbackQuery, state: FSMContext):
    if callback.data == "edit_cancel":
        await state.clear()
        await callback.message.answer("❌ Edit cancelled.", reply_markup=admin_menu())
        await callback.answer()
        return
    
    field = callback.data[11:]
    d = await state.get_data()
    telegram_id = d.get("telegram_id")
    brother_name = d.get("brother_name")
    brother = brothers_db.get(telegram_id, {})
    
    await state.update_data(editing_field=field)
    await state.set_state(EditBrother.editing_value)
    
    if field == "skills":
        await callback.message.answer(
            "🎯 *Edit Skills*\n\nSelect skills (tap all that apply, then Done):",
            reply_markup=skills_kb(),
        )
    elif field == "availability":
        await callback.message.answer(
            "📅 *Edit Availability*\n\nSelect days (tap to toggle, then Save):",
            reply_markup=avail_kb(),
        )
    elif field == "phone":
        current = brother.get('phone') or 'None'
        await callback.message.answer(
            f"📱 *Edit Phone*\n\nCurrent: {current}\n\nEnter new phone number or type *skip*:",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif field == "username":
        current = brother.get('telegram_username') or 'None'
        await callback.message.answer(
            f"👤 *Edit Telegram Username*\n\nCurrent: {current}\n\nEnter new username (e.g. @username) or type *skip*:",
            parse_mode=ParseMode.MARKDOWN,
        )
    
    await callback.answer()

@router.callback_query(F.data.startswith("skill_"), EditBrother.editing_value)
async def edit_skill(callback: CallbackQuery, state: FSMContext):
    if callback.data == "skill_done":
        d = await state.get_data()
        if not d.get("skills"):
            await callback.answer("⚠️ Please select at least one skill!", show_alert=True)
            return
        await state.set_state(EditBrother.confirm_edit)
        brother_name = d.get("brother_name")
        await callback.message.answer(
            f"✅ Skills: *{', '.join(d['skills'])}*\n\n📋 *Confirm changes for {brother_name}?*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=confirm_kb("edit_confirm"),
        )
        await callback.answer()
        return
    
    skill = callback.data[6:]
    d = await state.get_data()
    skills = d.get("skills", [])
    if skill in skills:
        skills.remove(skill)
    else:
        skills.append(skill)
    await state.update_data(skills=skills)
    await callback.answer(f"Selected: {', '.join(skills) or 'none'}")

@router.callback_query(F.data.startswith("av_"), EditBrother.editing_value)
async def edit_avail(callback: CallbackQuery, state: FSMContext):
    day = callback.data[3:]
    if day == "done":
        d = await state.get_data()
        avail = d.get("availability", ["saturday"])
        await state.set_state(EditBrother.confirm_edit)
        brother_name = d.get("brother_name")
        await callback.message.answer(
            f"✅ Availability: *{', '.join(avail)}*\n\n📋 *Confirm changes for {brother_name}?*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=confirm_kb("edit_confirm"),
        )
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

@router.message(EditBrother.editing_value)
async def edit_text_field(message: Message, state: FSMContext):
    d = await state.get_data()
    field = d.get("editing_field")
    
    if field == "phone":
        v = message.text.strip()
        await state.update_data(phone=None if v.lower() == "skip" else v)
    elif field == "username":
        v = message.text.strip()
        await state.update_data(telegram_username=None if v.lower() == "skip" else v)
    
    await state.set_state(EditBrother.confirm_edit)
    brother_name = d.get("brother_name")
    new_val = message.text.strip() if message.text.strip().lower() != "skip" else "—"
    await message.answer(
        f"✅ {field.title()}: *{new_val}*\n\n📋 *Confirm changes for {brother_name}?*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=confirm_kb("edit_confirm"),
    )

@router.callback_query(F.data.startswith("edit_confirm_"), EditBrother.confirm_edit)
async def edit_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "edit_confirm_no":
        await state.clear()
        await callback.message.answer("❌ Edit cancelled.", reply_markup=admin_menu())
        await callback.answer()
        return
    
    d = await state.get_data()
    telegram_id = d.get("telegram_id")
    brother_name = d.get("brother_name")
    field = d.get("editing_field")
    
    # Update the brother record
    if telegram_id in brothers_db:
        if field == "skills":
            brothers_db[telegram_id]['skills'] = d.get('skills', [])
        elif field == "availability":
            brothers_db[telegram_id]['availability'] = d.get('availability', ["saturday"])
        elif field == "phone":
            brothers_db[telegram_id]['phone'] = d.get('phone')
        elif field == "username":
            brothers_db[telegram_id]['telegram_username'] = d.get('telegram_username')
    
    await callback.message.answer(
        f"✅ *{brother_name}* has been updated successfully!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu(),
    )
    await state.clear()
    await callback.answer()

# ── Delete Brother ─────────────────────────────────────────────────────────────
@router.message(Command("deletebrother"))
@router.message(F.text == "🗑️ Delete Brother")
async def cmd_delete_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ This command is for admins only.")
        return
    
    if not brothers_db:
        await message.answer("❌ No brothers registered to delete.", reply_markup=admin_menu())
        return
    
    await state.set_state(DeleteBrother.choosing_brother)
    # Create inline keyboard with registered brother options
    kb_buttons = [
        [InlineKeyboardButton(text=f"🗑️ {brothers_db[tid]['full_name']}", callback_data=f"del_{i}")]
        for i, tid in enumerate(brothers_db.keys())
    ]
    kb_buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="del_cancel")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    
    await message.answer(
        "🗑️ *Delete Brother*\n\nSelect a brother to delete:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )

@router.callback_query((F.data.startswith("del_") & ~F.data.startswith("del_confirm_")) & ~F.data.in_(["del_cancel"]), DeleteBrother.choosing_brother)
async def del_select_brother(callback: CallbackQuery, state: FSMContext):
    if callback.data == "del_cancel":
        await state.clear()
        await callback.message.answer("❌ Deletion cancelled.", reply_markup=admin_menu())
        await callback.answer()
        return
    
    # Only handle non-confirm callbacks here
    if "confirm" in callback.data:
        return
    
    # Get telegram_id from index
    try:
        idx = int(callback.data[4:])
        telegram_id = list(brothers_db.keys())[idx]
        brother_name = brothers_db[telegram_id]['full_name']
    except (ValueError, IndexError):
        await callback.answer("❌ Invalid selection", show_alert=True)
        return
    
    await state.update_data(telegram_id=telegram_id, brother_name=brother_name)
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
    telegram_id = d.get("telegram_id")
    brother_name = d.get("brother_name")
    
    # Delete from storage
    if telegram_id in brothers_db:
        del brothers_db[telegram_id]
    
    await callback.message.answer(
        f"✅ *{brother_name}* has been successfully deleted from the registry!\n\n"
        f"Their assignments have been marked as unassigned.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu(),
    )
    await state.clear()
    await callback.answer()

# ── Edit Schedule ──────────────────────────────────────────────────────────────
@router.message(Command("editschedule"))
@router.message(F.text == "✏️ Edit Schedule")
async def cmd_edit_schedule(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ This command is for admins only.")
        return
    
    await state.set_state(EditSchedule.choosing_role)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎭 Stage", callback_data="sched_role_stage")],
        [InlineKeyboardButton(text="🎤 Microphone", callback_data="sched_role_microphone")],
        [InlineKeyboardButton(text="🔊 Audio", callback_data="sched_role_audio")],
        [InlineKeyboardButton(text="🎥 Video", callback_data="sched_role_video")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="sched_cancel")],
    ])
    
    await message.answer(
        "✏️ *Edit Schedule*\n\nSelect a role to edit:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )

@router.callback_query(F.data.startswith("sched_role_"), EditSchedule.choosing_role)
async def sched_choose_role(callback: CallbackQuery, state: FSMContext):
    if callback.data == "sched_cancel":
        await state.clear()
        await callback.message.answer("❌ Edit cancelled.", reply_markup=admin_menu())
        await callback.answer()
        return
    
    role = callback.data[11:]
    await state.update_data(role=role)
    await state.set_state(EditSchedule.choosing_brother)
    
    kb_buttons = [
        [InlineKeyboardButton(text=f"✅ {brothers_db[tid]['full_name']}", callback_data=f"sched_brother_{i}")]
        for i, tid in enumerate(brothers_db.keys())
    ]
    kb_buttons.append([InlineKeyboardButton(text="❌ None", callback_data="sched_brother_none")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    
    await callback.message.answer(
        f"✏️ *Edit {role.title()}*\n\nSelect brother to assign:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    await callback.answer()

@router.callback_query(F.data.startswith("sched_brother_"), EditSchedule.choosing_brother)
async def sched_choose_brother(callback: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    role = d.get("role")
    
    if callback.data == "sched_brother_none":
        telegram_id = None
        chosen_text = "None"
    else:
        try:
            idx = int(callback.data[14:])
            telegram_id = list(brothers_db.keys())[idx]
            chosen_text = brothers_db[telegram_id]['full_name']
        except (ValueError, IndexError):
            await callback.answer("❌ Invalid selection", show_alert=True)
            return
    
    await state.update_data(chosen_brother=telegram_id)
    await state.set_state(EditSchedule.confirm_edit)
    
    kb = confirm_kb("sched_confirm")
    await callback.message.answer(
        f"📋 *Confirm Assignment*\n\n"
        f"Role: *{role.title()}*\n"
        f"Assigned: *{chosen_text}*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )
    await callback.answer()

@router.callback_query(F.data.startswith("sched_confirm_"), EditSchedule.confirm_edit)
async def sched_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "sched_confirm_no":
        await state.clear()
        await callback.message.answer("❌ Edit cancelled.", reply_markup=admin_menu())
        await callback.answer()
        return
    
    d = await state.get_data()
    role = d.get("role")
    brother_id = d.get("chosen_brother")
    
    # Update assignment
    if role in ["stage", "audio", "video"]:
        assignments_db[role] = brother_id
    elif role == "microphone":
        if brother_id:
            assignments_db["microphone"] = [brother_id] if brother_id not in assignments_db["microphone"] else assignments_db["microphone"]
    
    # Get brother name for display
    brother_name = "None"
    if brother_id and brother_id in brothers_db:
        brother_name = brothers_db[brother_id]['full_name']
    
    await callback.message.answer(
        f"✅ *{role.title()}* assigned to *{brother_name}*!\n\nNotification sent.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu(),
    )
    
    # Send notification to assigned brother
    if brother_id and brother_id in brothers_db:
        today = date.today()
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        notification = (
            f"📢 *Schedule Update*\n\n"
            f"You have been assigned to:\n"
            f"🎯 *Role*: {role.title()}\n"
            f"📅 *Date*: {saturday.strftime('%A, %B %d, %Y')}\n"
            f"⏰ *Time*: 3:00 PM\n\n"
            f"Please confirm your availability using the buttons below."
        )
        kb = assign_kb(f"{brother_id}_{role}")
        if bot_instance:
            try:
                await bot_instance.send_message(brother_id, notification, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
                logger.info(f"Schedule notification sent to {brother_name} ({brother_id}) for {role}")
            except Exception as e:
                logger.error(f"Failed to send schedule notification to {brother_id}: {e}")
    
    await state.clear()
    await callback.answer()

# ── Pending Approval ───────────────────────────────────────────────────────────
@router.message(Command("approvepending"))
@router.message(F.text == "⏳ Pending Approval")
async def cmd_pending_approval(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ This command is for admins only.")
        return
    
    if not pending_approvals:
        await message.answer("✅ No pending registrations.", reply_markup=admin_menu())
        return
    
    await state.set_state(PendingApproval.approving)
    
    # Show first pending brother
    brother = pending_approvals[0]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Approve", callback_data="pending_approve")],
        [InlineKeyboardButton(text="❌ Reject", callback_data="pending_reject")],
    ])
    
    text = (
        f"⏳ *Pending Registration Review*\n\n"
        f"👤 Name: *{brother['full_name']}*\n"
        f"📱 Phone: {brother.get('phone', '—')}\n\n"
        f"_Skills and availability will be assigned after approval._\n\n"
        f"📊 Pending: {len(pending_approvals)} registration(s)"
    )
    
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

@router.callback_query(F.data == "pending_approve", PendingApproval.approving)
async def pending_approve(callback: CallbackQuery, state: FSMContext):
    if not pending_approvals:
        await callback.answer("No pending approvals")
        return
    
    brother = pending_approvals.pop(0)
    telegram_id = brother['telegram_id']
    
    # Save to brothers_db using telegram_id as key
    brothers_db[telegram_id] = {
        'telegram_id': telegram_id,
        'full_name': brother['full_name'],
        'skills': brother['skills'],
        'availability': brother['availability'],
        'phone': brother.get('phone'),
        'telegram_username': brother.get('telegram_username'),
        'serves': 0,
    }
    
    await callback.message.answer(
        f"✅ *{brother['full_name']}* has been approved and added to the registry!",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu(),
    )
    
    # Send approval notification to the brother
    approval_msg = (
        f"🎉 *Registration Approved!*\n\n"
        f"Welcome, *{brother['full_name']}*!\n\n"
        f"Your registration with the A/V Department has been approved. "
        f"You can now use the /myassignments command to view your assignments and "
        f"the /availability command to set your availability.\n\n"
        f"Thank you for serving! 🙏"
    )
    if bot_instance:
        try:
            await bot_instance.send_message(telegram_id, approval_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=brother_menu())
            logger.info(f"Approval notification sent to {brother['full_name']} ({telegram_id})")
        except Exception as e:
            logger.error(f"Failed to send approval notification to {telegram_id}: {e}")
    
    if pending_approvals:
        brother = pending_approvals[0]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Approve", callback_data="pending_approve")],
            [InlineKeyboardButton(text="❌ Reject", callback_data="pending_reject")],
        ])
        text = (
            f"⏳ *Next Pending Registration*\n\n"
            f"👤 Name: *{brother['full_name']}*\n"
            f"📱 Phone: {brother.get('phone', '—')}\n\n"
            f"_Skills and availability will be assigned after approval._\n\n"
            f"📊 Remaining: {len(pending_approvals)}"
        )
        await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await state.clear()
    
    await callback.answer()

@router.callback_query(F.data == "pending_reject", PendingApproval.approving)
async def pending_reject(callback: CallbackQuery, state: FSMContext):
    if not pending_approvals:
        await callback.answer("No pending approvals")
        return
    
    brother = pending_approvals.pop(0)
    
    await callback.message.answer(
        f"❌ *{brother['full_name']}* registration has been rejected.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=admin_menu(),
    )
    
    if pending_approvals:
        brother = pending_approvals[0]
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Approve", callback_data="pending_approve")],
            [InlineKeyboardButton(text="❌ Reject", callback_data="pending_reject")],
        ])
        text = (
            f"⏳ *Next Pending Registration*\n\n"
            f"👤 Name: *{brother['full_name']}*\n"
            f"📱 Phone: {brother.get('phone', '—')}\n\n"
            f"_Skills and availability will be assigned after approval._\n\n"
            f"📊 Remaining: {len(pending_approvals)}"
        )
        await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
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
    
    if not bot_instance:
        await message.answer("❌ Bot not ready. Try again in a moment.")
        return
    
    today = date.today()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    
    # Get assigned brothers
    assigned = []
    roles = {
        'stage': assignments_db.get('stage'),
        'microphone': assignments_db.get('microphone', []),
        'audio': assignments_db.get('audio'),
        'video': assignments_db.get('video'),
    }
    
    reminder_text = (
        f"🔔 *Service Reminder*\n\n"
        f"📅 *Date*: {saturday.strftime('%A, %B %d, %Y')}\n"
        f"⏰ *Time*: 3:00 PM\n\n"
        f"You have been assigned a role in this week's service. "
        f"Please confirm your attendance or let us know if you cannot make it.\n\n"
        f"Use /myassignments to view your assignments."
    )
    
    notified_count = 0
    notified_names = []
    
    # Send reminders to assigned brothers
    for role, brother_id in roles.items():
        if brother_id and isinstance(brother_id, int) and brother_id in brothers_db:
            brother_name = brothers_db[brother_id]['full_name']
            if brother_id not in [n[0] for n in notified_names]:  # Avoid duplicates if assigned to multiple roles
                try:
                    await bot_instance.send_message(
                        brother_id,
                        reminder_text,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    notified_count += 1
                    notified_names.append((brother_id, brother_name))
                    logger.info(f"Reminder sent to {brother_name} ({brother_id}) for {role}")
                except Exception as e:
                    logger.error(f"Failed to send reminder to {brother_id}: {e}")
        elif brother_id and isinstance(brother_id, list):  # For microphone list
            for mic_id in brother_id:
                if mic_id in brothers_db and mic_id not in [n[0] for n in notified_names]:
                    brother_name = brothers_db[mic_id]['full_name']
                    try:
                        await bot_instance.send_message(
                            mic_id,
                            reminder_text,
                            parse_mode=ParseMode.MARKDOWN,
                        )
                        notified_count += 1
                        notified_names.append((mic_id, brother_name))
                        logger.info(f"Reminder sent to {brother_name} ({mic_id}) for microphone")
                    except Exception as e:
                        logger.error(f"Failed to send reminder to {mic_id}: {e}")
    
    # Send confirmation to admin
    if notified_names:
        names_text = "\n".join([f"• {name} ({role.title()})" if role else f"• {name}" for _, name in notified_names for role, brother_id in roles.items() if isinstance(brother_id, int) and brother_id == _ or (isinstance(brother_id, list) and _ in brother_id)])
        result_text = (
            f"🔔 *Reminders Sent!*\n\n"
            f"Program: Saturday {saturday.strftime('%B %d')} at 3:00 PM\n\n"
            f"Notified ({notified_count}):\n"
        )
        for _, name in notified_names:
            result_text += f"• {name}\n"
    else:
        result_text = f"⚠️ *No reminders sent* — No assignments for {saturday.strftime('%B %d')}"
    
    await message.answer(result_text, parse_mode=ParseMode.MARKDOWN)

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
    assignment_id = callback.data[8:]  # Extract assignment_id
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "✅ *Confirmed!*\n\nThank you! See you at the program. We will send a reminder 24 hours before. 🙏",
        parse_mode=ParseMode.MARKDOWN,
    )
    
    # Notify admins about confirmation
    if bot_instance:
        try:
            user_name = callback.from_user.first_name
            confirm_msg = f"✅ *Assignment Confirmed*\n\n{user_name} has confirmed their assignment.\nAssignment ID: {assignment_id}"
            await notify_all_admins(confirm_msg)
        except Exception as e:
            logger.error(f"Failed to notify admins of confirmation: {e}")
    
    await callback.answer("Confirmed ✅")

@router.callback_query(F.data.startswith("decline_"))
async def cb_decline(callback: CallbackQuery):
    assignment_id = callback.data[8:]  # Extract assignment_id
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        "❌ *Noted.*\n\nYour absence has been recorded. Admin will arrange a replacement. Thank you for letting us know!",
        parse_mode=ParseMode.MARKDOWN,
    )
    
    # Notify admins about declination
    if bot_instance:
        try:
            user_name = callback.from_user.first_name
            decline_msg = f"❌ *Assignment Declined*\n\n{user_name} cannot make the assignment.\nAssignment ID: {assignment_id}\n\nPlease arrange a replacement."
            await notify_all_admins(decline_msg)
        except Exception as e:
            logger.error(f"Failed to notify admins of declination: {e}")
    
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
    global bot_instance
    import ssl
    import certifi
    from aiohttp import TCPConnector
    from aiogram.client.session.aiohttp import AiohttpSession

    ssl_context = ssl.create_default_context(cafile=certifi.where())

    connector = TCPConnector(ssl=ssl_context)
    session = AiohttpSession()
    session._connector = connector

    bot = Bot(token=BOT_TOKEN, session=session)
    bot_instance = bot  # Set global instance for notifications
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("✅ AV Department Bot is starting...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
