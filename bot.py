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

# ── Keyboards ─────────────────────────────────────────────────────────────────
def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 View Schedule"),   KeyboardButton(text="⚡ Auto-Schedule")],
            [KeyboardButton(text="👥 Brother List"),    KeyboardButton(text="➕ Register Brother")],
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

# ── Storage ───────────────────────────────────────────────────────────────────
brothers_db: dict = {}   # {full_name: {full_name, skills, availability, phone, telegram_username, serves}}
schedules_db: dict = {}  # {"latest": {date, assignments: {section:[names]}}, "pending_brothers":[names]}

# ── Router ────────────────────────────────────────────────────────────────────
router = Router()

# /start
@router.message(CommandStart())
async def cmd_start(message: Message):
    name  = message.from_user.first_name
    uid   = message.from_user.id
    uname = message.from_user.username  # may be None

    if is_admin(uid):
        await message.answer(
            f"👋 Welcome, *{name}*!\n\nYou have *Admin* access to the A/V Scheduling System.\nUse the menu below.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=admin_menu(),
        )
        return

    # Try to link this Telegram account to a registered brother.
    # Match by @username first, then by first name as a fallback.
    matched = None
    for b in brothers_db.values():
        tg = (b.get("telegram_username") or "").lstrip("@").lower()
        if uname and tg and tg == uname.lower():
            matched = b
            break
    if not matched:
        for b in brothers_db.values():
            if b["full_name"].split()[0].lower() == name.lower():
                matched = b
                break

    if matched:
        matched["telegram_id"] = uid
        await message.answer(
            f"👋 Welcome, *{matched['full_name']}*!\n\nYou are registered with the Audio & Video Department.\nUse the menu to view your assignments.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=brother_menu(),
        )
    else:
        await message.answer(
            f"👋 Hi *{name}*!\n\nYou are not yet registered in the A/V Department system.\nPlease ask an admin to register you.",
            parse_mode=ParseMode.MARKDOWN,
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
    # Save to in-memory store
    brothers_db[d["full_name"]] = {
        "full_name":          d["full_name"],
        "skills":             d.get("skills", []),
        "availability":       d.get("availability", ["saturday"]),
        "phone":              d.get("phone"),
        "telegram_username":  d.get("telegram_username"),
        "telegram_id":        None,   # linked when the brother messages the bot
        "serves":             0,
    }
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

    # Check if a schedule has been generated and saved
    schedule = schedules_db.get("latest")
    if not schedule:
        await message.answer(
            f"📅 *Saturday Service — {saturday.strftime('%B %d, %Y')}*\n\n"
            f"⚠️ No schedule has been generated yet.\n"
            f"Use ⚡ Auto-Schedule to create one.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    text = (
        f"📅 *Saturday Service*\n"
        f"🗓 {schedule['date']}  ⏰ 3:00 PM\n\n"
        f"👥 *Assignments:*\n"
    )
    icons = {"stage": "🎭", "microphone": "🎤", "audio": "🔊", "video": "🎥"}
    for section, names in schedule["assignments"].items():
        icon = icons.get(section, "•")
        label = section.capitalize()
        text += f"{icon} *{label}*: {', '.join(names) if names else '_(unassigned)_'}\n"

    if is_admin(message.from_user.id):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Approve",    callback_data="approve_schedule"),
            InlineKeyboardButton(text="⚡ Regenerate", callback_data="regen_schedule"),
        ]])
        await message.answer(text + "\n📌 *Status: Pending Approval*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
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
    if not brothers_db:
        await message.answer(
            "❌ No brothers registered yet.\nPlease register brothers first using ➕ Register Brother.",
            reply_markup=admin_menu(),
        )
        await state.clear()
        return

    await message.answer("⚙️ Generating fair schedule... please wait.")

    sections = {
        "stage":      {"required": 1, "assigned": []},
        "microphone": {"required": 2, "assigned": []},
        "audio":      {"required": 1, "assigned": []},
        "video":      {"required": 1, "assigned": []},
    }
    assigned_names: set[str] = set()
    warnings = []

    for section, info in sections.items():
        eligible = [
            b for b in sorted(brothers_db.values(), key=lambda x: x["serves"])
            if section in b["skills"] and b["full_name"] not in assigned_names
        ]
        picked = eligible[: info["required"]]
        if len(picked) < info["required"]:
            warnings.append(f"⚠️ Not enough brothers for {section} (need {info['required']}, found {len(picked)})")
        info["assigned"] = [b["full_name"] for b in picked]
        assigned_names.update(info["assigned"])

    icons = {"stage": "🎭", "microphone": "🎤", "audio": "🔊", "video": "🎥"}
    result = f"✅ *Schedule Generated!*\n\n📅 {prog_date.strftime('%A, %B %d %Y')}\n\n"
    for section, info in sections.items():
        names = ", ".join(info["assigned"]) if info["assigned"] else "_(unassigned)_"
        result += f"{icons[section]} *{section.capitalize()}*: {names}\n"
    if warnings:
        result += "\n" + "\n".join(warnings)

    # Save for later use by /viewschedule, /sendreminders, /report
    schedules_db["latest"] = {
        "date": prog_date.strftime("%B %d, %Y"),
        "prog_date": str(prog_date),
        "assignments": {s: info["assigned"] for s, info in sections.items()},
    }
    schedules_db["pending_brothers"] = list(assigned_names)

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
    for b in sorted(brothers_db.values(), key=lambda x: x["full_name"]):
        skills_str = ", ".join(b["skills"]) if b["skills"] else "None"
        text += f"🟢 *{b['full_name']}* — {skills_str} — {b['serves']} serves\n"
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ── My Assignments ─────────────────────────────────────────────────────────────
@router.message(Command("myassignments"))
@router.message(F.text == "📅 My Assignments")
async def cmd_my_assignments(message: Message):
    today = date.today()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    schedule = schedules_db.get("latest")

    if not schedule:
        await message.answer(
            "📅 *Your Upcoming Assignments*\n\nNo schedule has been generated yet.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Find this user's assignments by matching telegram_id → full_name
    user_id = message.from_user.id
    user_name = next(
        (b["full_name"] for b in brothers_db.values() if b.get("telegram_id") == user_id),
        None,
    )

    icons = {"stage": "🎭", "microphone": "🎤", "audio": "🔊", "video": "🎥"}
    assigned_sections = []
    if user_name:
        for section, names in schedule["assignments"].items():
            if user_name in names:
                assigned_sections.append(f"{icons.get(section, '•')} *{section.capitalize()}*")

    if assigned_sections:
        text = (
            f"📅 *Your Upcoming Assignments*\n\n"
            + "\n".join(assigned_sections)
            + f"\n\n📆 {schedule['date']} at 3:00 PM\n"
            f"Status: ⏳ Pending confirmation"
        )
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=assign_kb("latest"))
    else:
        text = (
            f"📅 *Your Upcoming Assignments*\n\n"
            f"You have no assignments for {schedule['date']}.\n"
            f"Check back after the next schedule is generated."
        )
        await message.answer(text, parse_mode=ParseMode.MARKDOWN)

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

    if not brothers_db:
        await message.answer(
            f"📊 *Monthly Report — {today.strftime('%B %Y')}*\n\nNo brothers registered yet.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    sorted_brothers = sorted(brothers_db.values(), key=lambda x: x["serves"], reverse=True)
    total_serves = sum(b["serves"] for b in brothers_db.values())

    text = f"📊 *Monthly Report — {today.strftime('%B %Y')}*\n\n"
    text += f"👥 Registered Brothers: *{len(brothers_db)}*\n"
    text += f"📝 Total Serves Recorded: *{total_serves}*\n\n"

    text += "*Serve Count (all time):*\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, b in enumerate(sorted_brothers):
        prefix = medals[i] if i < 3 else "•"
        text += f"{prefix} {b['full_name']} — {b['serves']} serves\n"

    low = [b for b in brothers_db.values() if b["serves"] == 0]
    if low:
        text += "\n*Not yet assigned:*\n"
        for b in low:
            text += f"• {b['full_name']}\n"

    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ── Send Reminders ─────────────────────────────────────────────────────────────
@router.message(Command("sendreminders"))
@router.message(F.text == "🔔 Send Reminders")
async def cmd_send_reminders(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ This command is for admins only.")
        return

    schedule = schedules_db.get("latest")
    if not schedule:
        await message.answer(
            "⚠️ No schedule found. Generate one first with ⚡ Auto-Schedule.",
            reply_markup=admin_menu(),
        )
        return

    icons = {"stage": "🎭", "microphone": "🎤", "audio": "🔊", "video": "🎥"}
    notified_lines = []
    for section, names in schedule["assignments"].items():
        for name in names:
            notified_lines.append(f"• {name} ({icons.get(section, '')} {section.capitalize()})")

    today = date.today()
    saturday = today + timedelta(days=(5 - today.weekday()) % 7)
    text = (
        f"🔔 *Reminders Sent!*\n\n"
        f"Program: {schedule['date']} at 3:00 PM\n\n"
        f"Notified:\n" + "\n".join(notified_lines) if notified_lines
        else "⚠️ No brothers assigned yet."
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
    schedule = schedules_db.get("latest")
    if not schedule:
        await callback.answer("No schedule to save.", show_alert=True)
        return

    icon_map = {"stage": "🎭", "microphone": "🎤", "audio": "🔊", "video": "🎥"}
    notified = []
    failed = []

    for section, names in schedule["assignments"].items():
        for name in names:
            if name in brothers_db:
                brothers_db[name]["serves"] += 1

            b = brothers_db.get(name)
            tid = b.get("telegram_id") if b else None
            section_label = icon_map.get(section, "") + " " + section.capitalize()

            if tid:
                try:
                    msg_parts = [
                        "📢 *You have been assigned!*",
                        "",
                        "📅 " + schedule["date"] + " at 3:00 PM",
                        "🎯 Your section: *" + section_label + "*",
                        "",
                        "Please confirm your attendance below.",
                    ]
                    await callback.bot.send_message(
                        chat_id=tid,
                        text=chr(10).join(msg_parts),
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=assign_kb(name + "_" + section),
                    )
                    notified.append("✅ " + name + " (" + section_label + ")")
                except Exception as e:
                    logger.warning("Failed to notify %s: %s", name, e)
                    failed.append("⚠️ " + name + " — delivery failed")
            else:
                failed.append("⚠️ " + name + " — no Telegram ID (hasn't started the bot yet)")

    all_results = notified + failed
    sep = chr(10)
    summary = sep.join(all_results) if all_results else "_(no brothers assigned)_"
    header = "💾 *Schedule Saved!*" + chr(10) + chr(10) + "*Notification Results:*" + chr(10)

    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        header + summary,
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
    bot = Bot(token=BOT_TOKEN)
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("✅ AV Department Bot is starting...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
