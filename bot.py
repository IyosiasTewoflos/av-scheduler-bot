"""
AV Department Scheduling Bot - v4
Multi-week scheduling: select start date + number of weeks (up to 8)
"""
from __future__ import annotations
import asyncio
import logging
import os
from datetime import date, timedelta
from pathlib import Path

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

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS  = set(map(int, os.getenv("ADMIN_IDS", "0").split(",")))

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    logger.error("BOT_TOKEN not set!")
    exit(1)

bot_instance: Bot = None

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def get_next_saturday() -> date:
    today = date.today()
    days_ahead = (5 - today.weekday()) % 7
    return today + timedelta(days=days_ahead)

def next_weekday(ref: date, weekday: int) -> date:
    """Return the next occurrence of weekday (0=Mon…6=Sun) on or after ref."""
    days_ahead = (weekday - ref.weekday()) % 7
    return ref + timedelta(days=days_ahead)

async def notify_admins(bot: Bot, text: str, kb=None):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception as e:
            logger.warning("Could not notify admin %s: %s", aid, e)

# ── Storage ───────────────────────────────────────────────────────────────────
brothers_db:       dict = {}   # {telegram_id: {...}}
pending_approvals: list = []   # [{telegram_id, full_name, ...}]
schedules_db:      dict = {}   # {iso_date_str: {program_date, week_label, assignments, approved}}

# ── FSM States ────────────────────────────────────────────────────────────────
class RegisterBrother(StatesGroup):
    full_name = State()
    phone     = State()
    skills    = State()
    confirm   = State()

class SelfRegister(StatesGroup):
    full_name = State()
    phone     = State()
    confirm   = State()

class AutoScheduleFlow(StatesGroup):
    pick_day     = State()   # which day of week
    pick_weeks   = State()   # how many weeks
    confirm      = State()   # preview before generating

class EditBrotherFlow(StatesGroup):
    choose_brother = State()
    choose_field   = State()
    enter_value    = State()
    choose_skills  = State()
    choose_avail   = State()

class DeleteBrotherFlow(StatesGroup):
    choose_brother = State()
    confirm        = State()

class EditScheduleFlow(StatesGroup):
    choose_week    = State()
    choose_section = State()
    choose_action  = State()
    pick_brother   = State()

class ViewScheduleFlow(StatesGroup):
    choose_week = State()

# ── Keyboards ─────────────────────────────────────────────────────────────────
WEEKDAY_NAMES = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
WEEKDAY_SHORT = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

def admin_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 View Schedule"),   KeyboardButton(text="⚡ Auto-Schedule")],
        [KeyboardButton(text="👥 Brother List"),    KeyboardButton(text="➕ Register Brother")],
        [KeyboardButton(text="✏️ Edit Brother"),    KeyboardButton(text="🗑 Delete Brother")],
        [KeyboardButton(text="📝 Edit Schedule"),   KeyboardButton(text="⏳ Pending Approvals")],
        [KeyboardButton(text="📊 Report"),           KeyboardButton(text="🔔 Send Reminders")],
    ], resize_keyboard=True)

def brother_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 My Assignments"),      KeyboardButton(text="✅ Set Availability")],
        [KeyboardButton(text="📋 This Week's Schedule")],
    ], resize_keyboard=True)

def skills_kb(selected: list | None = None) -> InlineKeyboardMarkup:
    selected = selected or []
    def lbl(s, icon):
        return f"✅ {icon} {s.capitalize()}" if s in selected else f"{icon} {s.capitalize()}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=lbl("stage","🎭"),      callback_data="skill_stage"),
         InlineKeyboardButton(text=lbl("microphone","🎤"), callback_data="skill_microphone")],
        [InlineKeyboardButton(text=lbl("audio","🔊"),      callback_data="skill_audio"),
         InlineKeyboardButton(text=lbl("video","🎥"),      callback_data="skill_video")],
        [InlineKeyboardButton(text="✅ Done", callback_data="skill_done")],
    ])

def avail_kb(selected: list | None = None) -> InlineKeyboardMarkup:
    selected = selected or []
    def lbl(d, label):
        return f"✅ {label}" if d in selected else label
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=lbl("saturday","Saturday"), callback_data="av_saturday"),
         InlineKeyboardButton(text=lbl("sunday","Sunday"),     callback_data="av_sunday")],
        [InlineKeyboardButton(text=lbl("weekday","Weekdays"),  callback_data="av_weekday")],
        [InlineKeyboardButton(text="💾 Save", callback_data="av_done")],
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

def brothers_list_kb(prefix: str, exclude_ids: list | None = None) -> InlineKeyboardMarkup:
    exclude_ids = exclude_ids or []
    rows = []
    for tid, b in sorted(brothers_db.items(), key=lambda x: x[1]["full_name"]):
        if tid not in exclude_ids:
            rows.append([InlineKeyboardButton(text=b["full_name"], callback_data=f"{prefix}:{tid}")])
    if not rows:
        rows.append([InlineKeyboardButton(text="(no brothers available)", callback_data="noop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def edit_fields_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Full Name",    callback_data="ef:full_name")],
        [InlineKeyboardButton(text="📱 Username",     callback_data="ef:username")],
        [InlineKeyboardButton(text="📞 Phone",        callback_data="ef:phone")],
        [InlineKeyboardButton(text="🎯 Skills",       callback_data="ef:skills")],
        [InlineKeyboardButton(text="📅 Availability", callback_data="ef:availability")],
        [InlineKeyboardButton(text="❌ Cancel",        callback_data="ef:cancel")],
    ])

def sections_kb() -> InlineKeyboardMarkup:
    icons = {"stage":"🎭","microphone":"🎤","audio":"🔊","video":"🎥"}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{icons[s]} {s.capitalize()}", callback_data=f"editsec:{s}")]
        for s in ["stage","microphone","audio","video"]
    ] + [[InlineKeyboardButton(text="❌ Cancel", callback_data="editsec:cancel")]])

# ── Day-of-week picker keyboard ───────────────────────────────────────────────
def day_picker_kb() -> InlineKeyboardMarkup:
    today = date.today()
    rows = []
    row = []
    for wd in range(7):  # 0=Mon … 6=Sun
        d = next_weekday(today, wd)
        label = f"{WEEKDAY_SHORT[wd]} {d.strftime('%b %d')}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"asday:{wd}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="asday:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── Weeks picker keyboard (1–8 weeks) ────────────────────────────────────────
def weeks_picker_kb(start_date: date, day_of_week: int) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for n in range(1, 9):
        end = start_date + timedelta(weeks=n - 1)
        label = f"{n} {'week' if n==1 else 'weeks'}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"asweeks:{n}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Back", callback_data="asweeks:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── Scheduled-weeks picker (for view / edit) ─────────────────────────────────
def scheduled_weeks_kb(prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for iso, sched in sorted(schedules_db.items()):
        d = date.fromisoformat(iso)
        label = f"📅 {d.strftime('%a %b %d')} — {sched['week_label']}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"{prefix}:{iso}")])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data=f"{prefix}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ── Format one week's schedule ────────────────────────────────────────────────
ICON = {"stage":"🎭","microphone":"🎤","audio":"🔊","video":"🎥"}

def format_one_schedule(iso: str, admin: bool = False) -> str:
    sched = schedules_db.get(iso)
    if not sched:
        return "Schedule not found."
    d = date.fromisoformat(iso)
    text = f"📅 *{sched['week_label']}*  🗓 {d.strftime('%A, %B %d %Y')}  ⏰ 3:00 PM\n\n"
    for section in ["stage","microphone","audio","video"]:
        ids = sched["assignments"].get(section, [])
        names = [brothers_db[tid]["full_name"] for tid in ids if tid in brothers_db]
        text += f"{ICON[section]} *{section.capitalize()}*: {', '.join(names) if names else '_(unassigned)_'}\n"
    if admin:
        status = "✅ Approved" if sched.get("approved") else "⏳ Pending Approval"
        text += f"\n📌 Status: {status}"
    return text

def generate_assignments_for_week(prog_date: date, already_served_recently: dict) -> tuple[dict, list]:
    """
    Assigns brothers to sections for one program date.
    already_served_recently: {tid: count_of_recent_serves} used for fairness across weeks.
    Returns (assignments_dict, warnings_list).
    """
    SECTIONS = [("stage",1),("microphone",2),("audio",1),("video",1)]
    # Sort by total serves + recent serves (fairest rotation)
    active = {tid: b for tid, b in brothers_db.items() if tid > 0}
    pool = sorted(active.values(),
                  key=lambda b: b["serves"] + already_served_recently.get(b["telegram_id"], 0))

    assignments: dict[str, list] = {}
    assigned_set: set = set()
    warnings: list = []

    for section, needed in SECTIONS:
        skilled = [b["telegram_id"] for b in pool
                   if b["telegram_id"] not in assigned_set
                   and section in b.get("skills", [])]
        any_avail = [b["telegram_id"] for b in pool
                     if b["telegram_id"] not in assigned_set]
        chosen = skilled[:needed]
        if len(chosen) < needed:
            extras = [tid for tid in any_avail if tid not in chosen]
            chosen += extras[:needed - len(chosen)]
            if len(chosen) < needed:
                warnings.append(f"⚠️ Not enough brothers for *{section}* on {prog_date.strftime('%b %d')}")
        assignments[section] = chosen
        assigned_set.update(chosen)
        # Track recent serves for fairness across weeks
        for tid in chosen:
            already_served_recently[tid] = already_served_recently.get(tid, 0) + 1

    return assignments, warnings

# ══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ══════════════════════════════════════════════════════════════════════════════
router = Router()

# ── /start ────────────────────────────────────────────────────────────────────
@router.message(CommandStart())
async def cmd_start(message: Message):
    uid  = message.from_user.id
    name = message.from_user.first_name
    if is_admin(uid):
        await message.answer(f"👋 Welcome, *{name}*!\n\nAdmin access.", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
        return
    if uid in brothers_db:
        await message.answer(f"👋 Welcome back, *{brothers_db[uid]['full_name']}*!", parse_mode=ParseMode.MARKDOWN, reply_markup=brother_menu())
        return
    already = any(p["telegram_id"] == uid for p in pending_approvals)
    if already:
        await message.answer(f"⏳ Hi *{name}*! Your registration is pending approval.", parse_mode=ParseMode.MARKDOWN)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Register", callback_data="self_register")]])
    await message.answer(f"👋 Hi *{name}*!\n\nNot yet registered. Would you like to register?", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ── Self-register ─────────────────────────────────────────────────────────────
@router.callback_query(F.data == "self_register")
async def self_reg_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SelfRegister.full_name)
    await callback.message.answer("📝 *Register*\n\nStep 1/2 — Your full name:", parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
    await callback.answer()

@router.message(SelfRegister.full_name)
async def self_reg_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip())
    await state.set_state(SelfRegister.phone)
    await message.answer("Step 2/2 — Phone number or *skip*:", parse_mode=ParseMode.MARKDOWN)

@router.message(SelfRegister.phone)
async def self_reg_phone(message: Message, state: FSMContext):
    v = message.text.strip()
    d = await state.get_data()
    phone = None if v.lower() == "skip" else v
    await state.update_data(phone=phone)
    await state.set_state(SelfRegister.confirm)
    await message.answer(
        f"📋 *Confirm*\n\n👤 *{d['full_name']}*\n📞 {phone or '—'}\n\nSubmit for admin approval?",
        parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_kb("selfreg"))

@router.callback_query(F.data.startswith("selfreg_"), SelfRegister.confirm)
async def self_reg_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "selfreg_no":
        await state.clear()
        await callback.message.answer("❌ Cancelled.")
        await callback.answer(); return
    d = await state.get_data()
    uid   = callback.from_user.id
    uname = callback.from_user.username
    entry = {"telegram_id": uid, "full_name": d["full_name"], "phone": d.get("phone"),
             "telegram_username": f"@{uname}" if uname else None,
             "skills": [], "availability": [], "serves": 0}
    pending_approvals.append(entry)
    await callback.message.answer("✅ Submitted! Admin will review shortly. 🙏", parse_mode=ParseMode.MARKDOWN)
    if bot_instance:
        await notify_admins(bot_instance,
            f"🔔 *New Registration Request*\n\n👤 *{d['full_name']}*\n📱 @{uname or '—'}\n🆔 `{uid}`\n\nUse *⏳ Pending Approvals* to review.")
    await state.clear()
    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# REGISTER BROTHER (admin)
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("registerbrother"))
@router.message(F.text == "➕ Register Brother")
async def reg_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin only."); return
    await state.set_state(RegisterBrother.full_name)
    await message.answer("📝 *Register New Brother*\n\nStep 1/3 — Full name:", parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())

@router.message(RegisterBrother.full_name)
async def reg_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip(), skills=[])
    await state.set_state(RegisterBrother.phone)
    await message.answer("Step 2/3 — Phone number or *skip*:", parse_mode=ParseMode.MARKDOWN)

@router.message(RegisterBrother.phone)
async def reg_phone(message: Message, state: FSMContext):
    v = message.text.strip()
    await state.update_data(phone=None if v.lower() == "skip" else v)
    await state.set_state(RegisterBrother.skills)
    await message.answer("Step 3/3 — Select skills:", reply_markup=skills_kb())

@router.callback_query(F.data.startswith("skill_"), RegisterBrother.skills)
async def reg_skill(callback: CallbackQuery, state: FSMContext):
    skill = callback.data[6:]
    if skill == "done":
        d = await state.get_data()
        if not d.get("skills"):
            await callback.answer("Select at least one skill!", show_alert=True); return
        await state.set_state(RegisterBrother.confirm)
        await callback.message.answer(
            f"📋 *Confirm Registration*\n\n👤 *{d['full_name']}*\n📞 {d.get('phone','—')}\n🎯 {', '.join(d['skills'])}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_kb("reg"))
        await callback.answer(); return
    d = await state.get_data()
    skills = d.get("skills", [])
    if skill in skills: skills.remove(skill)
    else: skills.append(skill)
    await state.update_data(skills=skills)
    await callback.message.edit_reply_markup(reply_markup=skills_kb(skills))
    await callback.answer()

@router.callback_query(F.data.startswith("reg_"), RegisterBrother.confirm)
async def reg_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "reg_no":
        await state.clear()
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu())
        await callback.answer(); return
    d = await state.get_data()
    import random
    fake_id = -(random.randint(100000, 999999))
    brothers_db[fake_id] = {
        "telegram_id": fake_id, "full_name": d["full_name"],
        "skills": d.get("skills", []), "availability": ["saturday"],
        "phone": d.get("phone"), "telegram_username": None,
        "serves": 0, "linked": False,
    }
    await callback.message.answer(
        f"✅ *{d['full_name']}* registered!\n\nAsk them to open the bot and send /start to link their account.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await state.clear()
    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# AUTO-SCHEDULE  ← multi-week with day picker
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("autoschedule"))
@router.message(F.text == "⚡ Auto-Schedule")
async def cmd_auto_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin only."); return
    active = {tid: b for tid, b in brothers_db.items() if tid > 0}
    if not active:
        await message.answer(
            "❌ *No linked brothers found.*\n\nBrothers must send /start first to link their account.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu()); return
    await state.set_state(AutoScheduleFlow.pick_day)
    await message.answer(
        "⚡ *Auto-Schedule*\n\n*Step 1/3 — Choose the service day of the week:*\n\n"
        "Tap the day your service program runs each week:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=ReplyKeyboardRemove())
    await message.answer("👇 Select day:", reply_markup=day_picker_kb())

@router.callback_query(F.data.startswith("asday:"), AutoScheduleFlow.pick_day)
async def auto_pick_day(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split(":")[1]
    if val == "cancel":
        await state.clear()
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu())
        await callback.answer(); return
    day_of_week = int(val)
    today = date.today()
    first_date = next_weekday(today, day_of_week)
    await state.update_data(day_of_week=day_of_week, first_date=first_date.isoformat())
    await state.set_state(AutoScheduleFlow.pick_weeks)
    day_name = WEEKDAY_NAMES[day_of_week]
    await callback.message.answer(
        f"✅ Day: *{day_name}*\n\n*Step 2/3 — How many weeks to schedule?*\n\n"
        f"First program: *{first_date.strftime('%A, %B %d')}*\n"
        f"Select number of weeks (1 week = 1 program, up to 8 weeks ≈ 2 months):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=weeks_picker_kb(first_date, day_of_week))
    await callback.answer()

@router.callback_query(F.data.startswith("asweeks:"), AutoScheduleFlow.pick_weeks)
async def auto_pick_weeks(callback: CallbackQuery, state: FSMContext):
    val = callback.data.split(":")[1]
    if val == "back":
        await state.set_state(AutoScheduleFlow.pick_day)
        await callback.message.answer("*Step 1/3 — Choose service day:", parse_mode=ParseMode.MARKDOWN, reply_markup=day_picker_kb())
        await callback.answer(); return
    num_weeks = int(val)
    d = await state.get_data()
    first_date = date.fromisoformat(d["first_date"])
    day_of_week = d["day_of_week"]
    # Build list of program dates
    dates = [first_date + timedelta(weeks=i) for i in range(num_weeks)]
    await state.update_data(num_weeks=num_weeks, program_dates=[dd.isoformat() for dd in dates])
    await state.set_state(AutoScheduleFlow.confirm)
    # Preview
    day_name = WEEKDAY_NAMES[day_of_week]
    preview = f"⚡ *Schedule Preview*\n\n*Step 3/3 — Confirm generation*\n\n"
    preview += f"📅 Day: *{day_name}*\n"
    preview += f"🔢 Weeks: *{num_weeks}*\n\n"
    preview += "*Programs to generate:*\n"
    for dd in dates:
        preview += f"  • {dd.strftime('%A, %B %d, %Y')}\n"
    preview += f"\n👥 Brothers in pool: *{len({t:b for t,b in brothers_db.items() if t>0})}*"
    preview += f"\n\nGenerate all {num_weeks} schedule{'s' if num_weeks>1 else ''}?"
    await callback.message.answer(preview, parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_kb("asconf"))
    await callback.answer()

@router.callback_query(F.data.startswith("asconf_"), AutoScheduleFlow.confirm)
async def auto_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "asconf_no":
        await state.clear()
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu())
        await callback.answer(); return

    d = await state.get_data()
    dates = [date.fromisoformat(iso) for iso in d["program_dates"]]
    await callback.message.answer("⚙️ Generating schedules...", parse_mode=ParseMode.MARKDOWN)

    recent_serves: dict = {}   # tracks fairness across weeks
    all_warnings: list = []
    generated_isos: list = []

    for prog_date in dates:
        assignments, warnings = generate_assignments_for_week(prog_date, recent_serves)
        iso = prog_date.isoformat()
        # Week label
        today = date.today()
        next_sat = get_next_saturday()
        diff_weeks = (prog_date - next_sat).days // 7
        if diff_weeks == 0:
            wl = f"This Week ({prog_date.strftime('%b %d')})"
        elif diff_weeks == 1:
            wl = f"Next Week ({prog_date.strftime('%b %d')})"
        else:
            wl = f"Week {diff_weeks+1} ({prog_date.strftime('%b %d')})"
        if diff_weeks < 0:
            wl = prog_date.strftime('%B %d, %Y')
        schedules_db[iso] = {
            "program_date": prog_date.strftime("%B %d, %Y"),
            "week_label": wl,
            "assignments": assignments,
            "approved": False,
        }
        generated_isos.append(iso)
        all_warnings.extend(warnings)

    # Build result summary
    result = f"✅ *{len(dates)} Schedule{'s' if len(dates)>1 else ''} Generated!*\n\n"
    for iso in generated_isos:
        result += format_one_schedule(iso, admin=True) + "\n"
        result += "─────────────────────\n"

    if all_warnings:
        result += "\n*Warnings:*\n" + "\n".join(all_warnings)

    # Buttons: approve all or save individually
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Save & Notify All", callback_data="saveall_schedules"),
         InlineKeyboardButton(text="🔄 Regenerate",        callback_data="regen_all")],
        [InlineKeyboardButton(text="📋 View Individual",   callback_data="viewindividual")],
    ])
    await state.update_data(generated_isos=generated_isos)
    # Store generated_isos in a temp key in schedules_db for the save callback
    schedules_db["_pending_generated"] = generated_isos
    await callback.message.answer(result, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    await state.clear()
    await callback.answer()

# ── Save all generated schedules and notify ───────────────────────────────────
@router.callback_query(F.data == "saveall_schedules")
async def cb_save_all(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Admin only", show_alert=True); return
    pending = schedules_db.pop("_pending_generated", list(schedules_db.keys()))
    notified_total, failed_total = [], []
    for iso in pending:
        sched = schedules_db.get(iso)
        if not sched: continue
        sched["approved"] = True
        for section, ids in sched["assignments"].items():
            for tid in ids:
                b = brothers_db.get(tid)
                if not b: continue
                label = f"{ICON.get(section,'')} {section.capitalize()}"
                if bot_instance and tid > 0:
                    try:
                        await bot_instance.send_message(
                            tid,
                            f"📢 *You have been assigned!*\n\n"
                            f"📅 {sched['week_label']}  🗓 {sched['program_date']}  ⏰ 3:00 PM\n"
                            f"🎯 Section: *{label}*\n\nPlease confirm:",
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=assign_kb(f"{tid}_{section}_{iso}"))
                        notified_total.append(f"✅ {b['full_name']} — {sched['week_label']}")
                        b["serves"] += 1
                    except Exception as e:
                        failed_total.append(f"⚠️ {b['full_name']} — failed ({sched['week_label']})")
                else:
                    failed_total.append(f"⚠️ {b['full_name']} — not linked ({sched['week_label']})")
    await callback.message.edit_reply_markup(reply_markup=None)
    lines = notified_total + failed_total
    await callback.message.answer(
        f"💾 *All Schedules Saved & Notifications Sent!*\n\n" + ("\n".join(lines) if lines else "_(none)_"),
        parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await callback.answer()

@router.callback_query(F.data == "regen_all")
async def cb_regen_all(callback: CallbackQuery):
    await callback.answer("Use ⚡ Auto-Schedule to regenerate.", show_alert=True)

@router.callback_query(F.data == "viewindividual")
async def cb_view_individual(callback: CallbackQuery, state: FSMContext):
    if not schedules_db:
        await callback.answer("No schedules.", show_alert=True); return
    await state.set_state(ViewScheduleFlow.choose_week)
    await callback.message.answer("📋 Select a week to view:", reply_markup=scheduled_weeks_kb("viewweek"))
    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# VIEW SCHEDULE
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("viewschedule"))
@router.message(F.text.in_({"📋 View Schedule","📋 This Week's Schedule"}))
async def cmd_view_schedule(message: Message, state: FSMContext):
    if not schedules_db:
        await message.answer("📋 No schedules yet. Use ⚡ Auto-Schedule.", parse_mode=ParseMode.MARKDOWN); return
    non_meta = {k:v for k,v in schedules_db.items() if not k.startswith("_")}
    if len(non_meta) == 1:
        iso = list(non_meta.keys())[0]
        is_adm = is_admin(message.from_user.id)
        text = format_one_schedule(iso, admin=is_adm)
        if is_adm:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Approve & Notify", callback_data=f"approve:{iso}"),
                InlineKeyboardButton(text="📝 Edit",             callback_data=f"goto_edit:{iso}"),
            ]])
            await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        else:
            await message.answer(text, parse_mode=ParseMode.MARKDOWN)
        return
    # Multiple weeks — show picker
    await state.set_state(ViewScheduleFlow.choose_week)
    await message.answer(
        f"📋 *{len(non_meta)} schedules available.*\n\nSelect a week to view:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=scheduled_weeks_kb("viewweek"))

@router.callback_query(F.data.startswith("viewweek:"), ViewScheduleFlow.choose_week)
async def view_week_chosen(callback: CallbackQuery, state: FSMContext):
    iso = callback.data.split(":")[1]
    if iso == "cancel":
        await state.clear()
        await callback.message.answer("Cancelled.", reply_markup=admin_menu() if is_admin(callback.from_user.id) else brother_menu())
        await callback.answer(); return
    await state.clear()
    is_adm = is_admin(callback.from_user.id)
    text = format_one_schedule(iso, admin=is_adm)
    if is_adm:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Approve & Notify", callback_data=f"approve:{iso}"),
            InlineKeyboardButton(text="📝 Edit",             callback_data=f"goto_edit:{iso}"),
        ]])
        await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await callback.message.answer(text, parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# APPROVE SCHEDULE
# ══════════════════════════════════════════════════════════════════════════════
@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Admin only", show_alert=True); return
    iso = callback.data.split(":")[1]
    sched = schedules_db.get(iso)
    if not sched:
        await callback.answer("Schedule not found.", show_alert=True); return
    sched["approved"] = True
    notified = []
    for section, ids in sched["assignments"].items():
        for tid in ids:
            b = brothers_db.get(tid)
            if not b: continue
            label = f"{ICON.get(section,'')} {section.capitalize()}"
            if bot_instance and tid > 0:
                try:
                    await bot_instance.send_message(
                        tid,
                        f"📢 *You have been assigned!*\n\n"
                        f"📅 {sched['week_label']}  🗓 {sched['program_date']}  ⏰ 3:00 PM\n"
                        f"🎯 Section: *{label}*\n\nPlease confirm:",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=assign_kb(f"{tid}_{section}_{iso}"))
                    notified.append(b["full_name"])
                    b["serves"] += 1
                except Exception: pass
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"✅ *Approved!*\n\nNotified: {', '.join(notified) or '(none linked)'}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# EDIT SCHEDULE
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("editschedule"))
@router.message(F.text == "📝 Edit Schedule")
async def edit_schedule_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin only."); return
    non_meta = {k:v for k,v in schedules_db.items() if not k.startswith("_")}
    if not non_meta:
        await message.answer("⚠️ No schedules yet."); return
    await state.set_state(EditScheduleFlow.choose_week)
    await message.answer("📝 *Edit Schedule*\n\nSelect week to edit:", parse_mode=ParseMode.MARKDOWN, reply_markup=scheduled_weeks_kb("editweek"))

@router.callback_query(F.data.startswith("goto_edit:"))
async def cb_goto_edit(callback: CallbackQuery, state: FSMContext):
    iso = callback.data.split(":")[1]
    await state.update_data(editing_iso=iso)
    await state.set_state(EditScheduleFlow.choose_section)
    sched = schedules_db.get(iso, {})
    await callback.message.answer(
        f"📝 *Edit — {sched.get('week_label','?')}*\n\nChoose section:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=sections_kb())
    await callback.answer()

@router.callback_query(F.data.startswith("editweek:"), EditScheduleFlow.choose_week)
async def edit_week_chosen(callback: CallbackQuery, state: FSMContext):
    iso = callback.data.split(":")[1]
    if iso == "cancel":
        await state.clear()
        await callback.message.answer("Cancelled.", reply_markup=admin_menu())
        await callback.answer(); return
    await state.update_data(editing_iso=iso)
    await state.set_state(EditScheduleFlow.choose_section)
    sched = schedules_db.get(iso, {})
    await callback.message.answer(
        f"📝 *Edit — {sched.get('week_label','?')}*\n\nChoose section to edit:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=sections_kb())
    await callback.answer()

@router.callback_query(F.data.startswith("editsec:"), EditScheduleFlow.choose_section)
async def edit_sec_chosen(callback: CallbackQuery, state: FSMContext):
    section = callback.data.split(":")[1]
    if section == "cancel":
        await state.clear()
        await callback.message.answer("Cancelled.", reply_markup=admin_menu())
        await callback.answer(); return
    d = await state.get_data()
    iso = d["editing_iso"]
    sched = schedules_db[iso]
    await state.update_data(edit_section=section)
    current_ids = sched["assignments"].get(section, [])
    current_names = [brothers_db[tid]["full_name"] for tid in current_ids if tid in brothers_db]
    rows = []
    for tid in current_ids:
        b = brothers_db.get(tid)
        if b:
            rows.append([InlineKeyboardButton(text=f"🔄 Replace {b['full_name']}", callback_data=f"editact:replace:{tid}")])
            rows.append([InlineKeyboardButton(text=f"❌ Remove {b['full_name']}",  callback_data=f"editact:remove:{tid}")])
    rows.append([InlineKeyboardButton(text="➕ Add brother", callback_data="editact:add:none")])
    rows.append([InlineKeyboardButton(text="⬅️ Back",        callback_data="editact:back:none")])
    await state.set_state(EditScheduleFlow.choose_action)
    await callback.message.answer(
        f"{ICON.get(section,'•')} *{section.capitalize()}* — {', '.join(current_names) or '(empty)'}\n\nAction:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()

@router.callback_query(F.data.startswith("editact:"), EditScheduleFlow.choose_action)
async def edit_action_chosen(callback: CallbackQuery, state: FSMContext):
    _, action, target = callback.data.split(":", 2)
    d = await state.get_data()
    iso = d["editing_iso"]
    section = d["edit_section"]
    if action == "back":
        await state.set_state(EditScheduleFlow.choose_section)
        await callback.message.answer("Choose section:", reply_markup=sections_kb())
        await callback.answer(); return
    if action == "remove":
        tid = int(target)
        ids = schedules_db[iso]["assignments"].get(section, [])
        if tid in ids: ids.remove(tid)
        name = brothers_db.get(tid, {}).get("full_name", str(tid))
        await state.clear()
        await callback.message.answer(
            f"🗑 *{name}* removed from *{section.capitalize()}*.\n\n" + format_one_schedule(iso, admin=True),
            parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
        await callback.answer(); return
    # replace or add
    await state.update_data(edit_action=action, replacing_tid=int(target) if target != "none" else None)
    await state.set_state(EditScheduleFlow.pick_brother)
    all_assigned = [tid for s, ids in schedules_db[iso]["assignments"].items() for tid in ids if s != section]
    await callback.message.answer(
        f"Select a brother for *{section.capitalize()}*:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=brothers_list_kb("picknew", exclude_ids=all_assigned))
    await callback.answer()

@router.callback_query(F.data.startswith("picknew:"), EditScheduleFlow.pick_brother)
async def edit_pick_new(callback: CallbackQuery, state: FSMContext):
    new_tid = int(callback.data.split(":")[1])
    d = await state.get_data()
    iso = d["editing_iso"]
    section = d["edit_section"]
    action = d["edit_action"]
    replacing_tid = d.get("replacing_tid")
    ids = schedules_db[iso]["assignments"].setdefault(section, [])
    if action == "replace" and replacing_tid in ids:
        ids[ids.index(replacing_tid)] = new_tid
    else:
        if new_tid not in ids: ids.append(new_tid)
    new_name = brothers_db.get(new_tid, {}).get("full_name", str(new_tid))
    await state.clear()
    await callback.message.answer(
        f"✅ *{new_name}* assigned to *{section.capitalize()}*.\n\n" + format_one_schedule(iso, admin=True),
        parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# EDIT BROTHER
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("editbrother"))
@router.message(F.text == "✏️ Edit Brother")
async def edit_bro_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): await message.answer("❌ Admin only."); return
    if not brothers_db: await message.answer("No brothers yet."); return
    await state.set_state(EditBrotherFlow.choose_brother)
    await message.answer("✏️ *Edit Brother*\n\nSelect:", parse_mode=ParseMode.MARKDOWN, reply_markup=brothers_list_kb("editbro"))

@router.callback_query(F.data.startswith("editbro:"), EditBrotherFlow.choose_brother)
async def edit_bro_chosen(callback: CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":")[1])
    b = brothers_db.get(tid)
    if not b: await callback.answer("Not found.", show_alert=True); return
    await state.update_data(editing_tid=tid)
    await state.set_state(EditBrotherFlow.choose_field)
    await callback.message.answer(
        f"✏️ *{b['full_name']}*\n🎯 {', '.join(b['skills']) or 'None'}\n📞 {b.get('phone','—')}\n\nWhat to change?",
        parse_mode=ParseMode.MARKDOWN, reply_markup=edit_fields_kb())
    await callback.answer()

@router.callback_query(F.data.startswith("ef:"), EditBrotherFlow.choose_field)
async def edit_field_chosen(callback: CallbackQuery, state: FSMContext):
    field = callback.data[3:]
    if field == "cancel":
        await state.clear(); await callback.message.answer("Cancelled.", reply_markup=admin_menu()); await callback.answer(); return
    d = await state.get_data()
    b = brothers_db[d["editing_tid"]]
    await state.update_data(edit_field=field)
    if field == "skills":
        await state.set_state(EditBrotherFlow.choose_skills)
        await state.update_data(new_skills=b["skills"].copy())
        await callback.message.answer("🎯 Toggle skills:", reply_markup=skills_kb(b["skills"]))
    elif field == "availability":
        await state.set_state(EditBrotherFlow.choose_avail)
        await state.update_data(new_avail=b.get("availability",[]).copy())
        await callback.message.answer("📅 Toggle availability:", reply_markup=avail_kb(b.get("availability",[])))
    else:
        await state.set_state(EditBrotherFlow.enter_value)
        labels = {"full_name":"Full Name","username":"Telegram Username","phone":"Phone"}
        current = b.get(field if field != "username" else "telegram_username","—")
        await callback.message.answer(f"Enter new *{labels.get(field,field)}* (current: {current}):", parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
    await callback.answer()

@router.callback_query(F.data.startswith("skill_"), EditBrotherFlow.choose_skills)
async def edit_bro_skill(callback: CallbackQuery, state: FSMContext):
    skill = callback.data[6:]
    d = await state.get_data()
    skills = d.get("new_skills", [])
    if skill == "done":
        if not skills: await callback.answer("Select at least one!", show_alert=True); return
        brothers_db[d["editing_tid"]]["skills"] = skills
        await state.clear()
        await callback.message.answer(f"✅ Skills: *{', '.join(skills)}*", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
        await callback.answer(); return
    if skill in skills: skills.remove(skill)
    else: skills.append(skill)
    await state.update_data(new_skills=skills)
    await callback.message.edit_reply_markup(reply_markup=skills_kb(skills))
    await callback.answer()

@router.callback_query(F.data.startswith("av_"), EditBrotherFlow.choose_avail)
async def edit_bro_avail(callback: CallbackQuery, state: FSMContext):
    day = callback.data[3:]
    d = await state.get_data()
    avail = d.get("new_avail", [])
    if day == "done":
        brothers_db[d["editing_tid"]]["availability"] = avail
        await state.clear()
        await callback.message.answer(f"✅ Availability: *{', '.join(avail) or 'None'}*", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
        await callback.answer(); return
    if day in avail: avail.remove(day)
    else: avail.append(day)
    await state.update_data(new_avail=avail)
    await callback.message.edit_reply_markup(reply_markup=avail_kb(avail))
    await callback.answer()

@router.message(EditBrotherFlow.enter_value)
async def edit_bro_value(message: Message, state: FSMContext):
    d = await state.get_data()
    tid, field, val = d["editing_tid"], d["edit_field"], message.text.strip()
    if field == "full_name": brothers_db[tid]["full_name"] = val
    elif field == "username": brothers_db[tid]["telegram_username"] = val
    elif field == "phone": brothers_db[tid]["phone"] = val if val.lower() != "skip" else None
    await state.clear()
    await message.answer("✅ Updated!", reply_markup=admin_menu())

# ══════════════════════════════════════════════════════════════════════════════
# DELETE BROTHER
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("deletebrother"))
@router.message(F.text == "🗑 Delete Brother")
async def del_bro_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): await message.answer("❌ Admin only."); return
    if not brothers_db: await message.answer("No brothers registered."); return
    await state.set_state(DeleteBrotherFlow.choose_brother)
    await message.answer("🗑 *Delete Brother*\n\nSelect:", parse_mode=ParseMode.MARKDOWN, reply_markup=brothers_list_kb("delbro"))

@router.callback_query(F.data.startswith("delbro:"), DeleteBrotherFlow.choose_brother)
async def del_bro_chosen(callback: CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":")[1])
    b = brothers_db.get(tid)
    if not b: await callback.answer("Not found.", show_alert=True); return
    await state.update_data(deleting_tid=tid)
    await state.set_state(DeleteBrotherFlow.confirm)
    await callback.message.answer(
        f"⚠️ *Confirm Delete*\n\n👤 *{b['full_name']}*\n🎯 {', '.join(b['skills']) or 'None'}\n📊 {b['serves']} serves\n\nThis cannot be undone!",
        parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_kb("del"))
    await callback.answer()

@router.callback_query(F.data.startswith("del_"), DeleteBrotherFlow.confirm)
async def del_bro_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "del_no":
        await state.clear(); await callback.message.answer("Cancelled.", reply_markup=admin_menu()); await callback.answer(); return
    d = await state.get_data()
    tid = d["deleting_tid"]
    name = brothers_db.get(tid, {}).get("full_name","?")
    brothers_db.pop(tid, None)
    for sched in schedules_db.values():
        if isinstance(sched, dict) and "assignments" in sched:
            for ids in sched["assignments"].values():
                if tid in ids: ids.remove(tid)
    await state.clear()
    await callback.message.answer(f"🗑 *{name}* deleted.", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# PENDING APPROVALS
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("pendingapprovals"))
@router.message(F.text == "⏳ Pending Approvals")
async def cmd_pending(message: Message):
    if not is_admin(message.from_user.id): await message.answer("❌ Admin only."); return
    if not pending_approvals:
        await message.answer("✅ No pending approvals.", reply_markup=admin_menu()); return
    text = f"⏳ *Pending Approvals ({len(pending_approvals)})*\n\n"
    buttons = []
    for p in pending_approvals:
        text += f"👤 *{p['full_name']}*  📱 {p.get('telegram_username','—')}  🆔 `{p['telegram_id']}`\n\n"
        buttons.append([
            InlineKeyboardButton(text=f"✅ Approve {p['full_name'].split()[0]}", callback_data=f"papprove:{p['telegram_id']}"),
            InlineKeyboardButton(text="❌ Reject", callback_data=f"preject:{p['telegram_id']}"),
        ])
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("papprove:"))
async def cb_papprove(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): await callback.answer("Admin only", show_alert=True); return
    tid = int(callback.data.split(":")[1])
    entry = next((p for p in pending_approvals if p["telegram_id"]==tid), None)
    if not entry: await callback.answer("Already handled.", show_alert=True); return
    pending_approvals.remove(entry)
    brothers_db[tid] = {**entry, "linked": True}
    if bot_instance:
        try:
            await bot_instance.send_message(tid, f"🎉 *Approved!* Welcome, *{entry['full_name']}*! 🙏", parse_mode=ParseMode.MARKDOWN, reply_markup=brother_menu())
        except Exception: pass
    await callback.message.answer(f"✅ *{entry['full_name']}* approved!", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await callback.answer()

@router.callback_query(F.data.startswith("preject:"))
async def cb_preject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): await callback.answer("Admin only", show_alert=True); return
    tid = int(callback.data.split(":")[1])
    entry = next((p for p in pending_approvals if p["telegram_id"]==tid), None)
    if not entry: await callback.answer("Already handled.", show_alert=True); return
    pending_approvals.remove(entry)
    if bot_instance:
        try: await bot_instance.send_message(tid, "❌ Registration not approved. Contact admin.")
        except Exception: pass
    await callback.message.answer(f"❌ *{entry['full_name']}* rejected.", parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# BROTHER LIST / MY ASSIGNMENTS / AVAILABILITY / REPORT / REMINDERS
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("brotherlist"))
@router.message(F.text == "👥 Brother List")
async def cmd_brother_list(message: Message):
    if not is_admin(message.from_user.id): await message.answer("❌ Admin only."); return
    if not brothers_db: await message.answer("No brothers yet.", reply_markup=admin_menu()); return
    text = "👥 *Brothers Registry*\n\n"
    for b in sorted(brothers_db.values(), key=lambda x: x["full_name"]):
        linked = "🟢" if b.get("linked", b["telegram_id"]>0) else "🟡"
        text += f"{linked} *{b['full_name']}* — {', '.join(b['skills']) or 'No skills'} — {b['serves']} serves\n"
    text += "\n🟢 Linked  🟡 Not yet linked"
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("myassignments"))
@router.message(F.text == "📅 My Assignments")
async def cmd_my_assignments(message: Message):
    uid = message.from_user.id
    non_meta = {k:v for k,v in schedules_db.items() if not k.startswith("_")}
    if not non_meta:
        await message.answer("No schedules generated yet."); return
    my_assignments = []
    for iso, sched in sorted(non_meta.items()):
        for section, ids in sched["assignments"].items():
            if uid in ids:
                my_assignments.append((iso, sched, section))
    if my_assignments:
        text = "📅 *Your Upcoming Assignments*\n\n"
        for iso, sched, section in my_assignments:
            text += f"{ICON.get(section,'•')} *{section.capitalize()}*  —  {sched['week_label']}\n  🗓 {sched['program_date']}  ⏰ 3:00 PM\n\n"
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=assign_kb(str(uid)))
    else:
        await message.answer("📅 You have no upcoming assignments.", parse_mode=ParseMode.MARKDOWN)

@router.message(Command("availability"))
@router.message(F.text == "✅ Set Availability")
async def cmd_availability(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Available this Saturday", callback_data="avail_yes"),
        InlineKeyboardButton(text="❌ Not available",           callback_data="avail_no"),
    ]])
    await message.answer("📅 Available this Saturday?", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

@router.message(Command("report"))
@router.message(F.text == "📊 Report")
async def cmd_report(message: Message):
    if not is_admin(message.from_user.id): await message.answer("❌ Admin only."); return
    today = date.today()
    if not brothers_db:
        await message.answer(f"📊 No data yet.", reply_markup=admin_menu()); return
    sorted_b = sorted(brothers_db.values(), key=lambda x: x["serves"], reverse=True)
    non_meta = {k:v for k,v in schedules_db.items() if not k.startswith("_")}
    text = (f"📊 *Report — {today.strftime('%B %Y')}*\n\n"
            f"👥 Brothers: *{len(brothers_db)}*\n"
            f"📅 Schedules: *{len(non_meta)}*\n"
            f"📝 Total Serves: *{sum(b['serves'] for b in brothers_db.values())}*\n\n"
            f"*Serve Count:*\n")
    medals = ["🥇","🥈","🥉"]
    for i, b in enumerate(sorted_b):
        text += f"{medals[i] if i<3 else '•'} {b['full_name']} — {b['serves']} serves\n"
    zero = [b for b in brothers_db.values() if b["serves"]==0]
    if zero:
        text += "\n*Not yet assigned:*\n" + "".join(f"• {b['full_name']}\n" for b in zero)
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("sendreminders"))
@router.message(F.text == "🔔 Send Reminders")
async def cmd_reminders(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): await message.answer("❌ Admin only."); return
    non_meta = {k:v for k,v in schedules_db.items() if not k.startswith("_")}
    if not non_meta: await message.answer("No schedules yet."); return
    if len(non_meta) == 1:
        iso = list(non_meta.keys())[0]
        await send_reminders_for(message, iso)
    else:
        await state.set_state(ViewScheduleFlow.choose_week)
        await message.answer("🔔 Send reminders for which week?", reply_markup=scheduled_weeks_kb("remindweek"))

@router.callback_query(F.data.startswith("remindweek:"), ViewScheduleFlow.choose_week)
async def cb_remind_week(callback: CallbackQuery, state: FSMContext):
    iso = callback.data.split(":")[1]
    if iso == "cancel":
        await state.clear(); await callback.message.answer("Cancelled.", reply_markup=admin_menu()); await callback.answer(); return
    await state.clear()
    await send_reminders_for(callback.message, iso)
    await callback.answer()

async def send_reminders_for(message: Message, iso: str):
    sched = schedules_db.get(iso)
    if not sched: await message.answer("Schedule not found."); return
    sent, failed = [], []
    for section, ids in sched["assignments"].items():
        for tid in ids:
            b = brothers_db.get(tid)
            if not b: continue
            label = f"{ICON.get(section,'')} {section.capitalize()}"
            if bot_instance and tid > 0:
                try:
                    await bot_instance.send_message(
                        tid,
                        f"⏰ *Reminder!*\n\n📅 {sched['week_label']}\n🗓 {sched['program_date']}  ⏰ 3:00 PM\n🎯 Section: *{label}*\n\nSee you there! 🙏",
                        parse_mode=ParseMode.MARKDOWN)
                    sent.append(f"✅ {b['full_name']} ({label})")
                except Exception:
                    failed.append(f"⚠️ {b['full_name']} — failed")
            else:
                failed.append(f"⚠️ {b['full_name']} — not linked yet")
    lines = sent + failed
    await message.answer(
        f"🔔 *Reminders — {sched['week_label']}*\n\n" + ("\n".join(lines) if lines else "_(none)_"),
        parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())

# ══════════════════════════════════════════════════════════════════════════════
# GENERAL CALLBACKS
# ══════════════════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "regen_all")
async def cb_regen(callback: CallbackQuery):
    await callback.answer("Use ⚡ Auto-Schedule to regenerate.", show_alert=True)

@router.callback_query(F.data.startswith("confirm_"))
async def cb_confirm(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ *Confirmed!* See you at the program. 🙏", parse_mode=ParseMode.MARKDOWN)
    if bot_instance:
        name = callback.from_user.first_name
        await notify_admins(bot_instance, f"✅ *{name}* confirmed their assignment.")
    await callback.answer("Confirmed ✅")

@router.callback_query(F.data.startswith("decline_"))
async def cb_decline(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("❌ *Noted.* Admin will arrange a replacement. Thank you!", parse_mode=ParseMode.MARKDOWN)
    if bot_instance:
        name = callback.from_user.first_name
        await notify_admins(bot_instance, f"❌ *{name}* declined their assignment. Please arrange a replacement.")
    await callback.answer()

@router.callback_query(F.data.startswith("avail_"))
async def cb_avail(callback: CallbackQuery):
    msg = "✅ *Marked available!*" if callback.data == "avail_yes" else "❌ *Marked unavailable.* 🙏"
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(msg, parse_mode=ParseMode.MARKDOWN)
    await callback.answer()

@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
async def main():
    global bot_instance
    bot = Bot(token=BOT_TOKEN)
    bot_instance = bot
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("AV Department Bot v4 starting...")
    await dp.start_polling(bot, allowed_updates=["message","callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
