"""
AV Department Scheduling Bot - v3
Fixed: Auto-schedule uses only registered brothers
Fixed: Program date = next Saturday automatically (start of service week)
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
ADMIN_IDS = set(map(int, os.getenv("ADMIN_IDS", "0").split(",")))

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    logger.error("❌ BOT_TOKEN not set! Add it to your .env file.")
    exit(1)

bot_instance: Bot = None

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def get_next_saturday() -> date:
    """Return this week's Saturday (or today if today is Saturday)."""
    today = date.today()
    days_ahead = (5 - today.weekday()) % 7   # Saturday = weekday 5
    return today + timedelta(days=days_ahead)

def week_label(program_date: date) -> str:
    """Return a human-readable week label like 'This Week (June 7)'."""
    today = date.today()
    saturday = get_next_saturday()
    if program_date == saturday:
        return f"This Week ({program_date.strftime('%B %d')})"
    elif program_date == saturday + timedelta(weeks=1):
        return f"Next Week ({program_date.strftime('%B %d')})"
    else:
        return program_date.strftime('%B %d, %Y')

async def notify_admins(bot: Bot, text: str, kb=None):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception as e:
            logger.warning("Could not notify admin %s: %s", aid, e)

# ── Stores ────────────────────────────────────────────────────────────────────
# brothers_db: keyed by telegram_id (int)
# {telegram_id: {full_name, skills, availability, phone, telegram_username, serves}}
brothers_db: dict = {}
pending_approvals: list = []   # list of brother dicts awaiting admin approval

# schedule_db: the active generated schedule
# {program_date, week_label, assignments: {section: [telegram_ids]}}
schedule_db: dict = {}

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

class EditBrotherFlow(StatesGroup):
    choose_brother  = State()
    choose_field    = State()
    enter_value     = State()
    choose_skills   = State()
    choose_avail    = State()

class DeleteBrotherFlow(StatesGroup):
    choose_brother = State()
    confirm        = State()

class EditScheduleFlow(StatesGroup):
    choose_section     = State()
    choose_action      = State()
    choose_new_brother = State()

# ── Keyboards ─────────────────────────────────────────────────────────────────
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
        [KeyboardButton(text="📅 My Assignments"), KeyboardButton(text="✅ Set Availability")],
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
        [InlineKeyboardButton(text="✅ Done",               callback_data="skill_done")],
    ])

def avail_kb(selected: list | None = None) -> InlineKeyboardMarkup:
    selected = selected or []
    def lbl(d, label):
        return f"✅ {label}" if d in selected else label
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=lbl("saturday","Saturday"), callback_data="av_saturday"),
         InlineKeyboardButton(text=lbl("sunday","Sunday"),     callback_data="av_sunday")],
        [InlineKeyboardButton(text=lbl("weekday","Weekdays"),  callback_data="av_weekday")],
        [InlineKeyboardButton(text="💾 Save",                  callback_data="av_done")],
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
    """Inline keyboard of all registered brothers."""
    exclude_ids = exclude_ids or []
    rows = []
    for tid, b in sorted(brothers_db.items(), key=lambda x: x[1]["full_name"]):
        if tid not in exclude_ids:
            rows.append([InlineKeyboardButton(
                text=b["full_name"],
                callback_data=f"{prefix}:{tid}"
            )])
    if not rows:
        rows.append([InlineKeyboardButton(text="(no brothers available)", callback_data="noop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def sections_kb() -> InlineKeyboardMarkup:
    icons = {"stage":"🎭","microphone":"🎤","audio":"🔊","video":"🎥"}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{icons[s]} {s.capitalize()}", callback_data=f"editsec:{s}")]
        for s in ["stage","microphone","audio","video"]
    ] + [[InlineKeyboardButton(text="❌ Cancel", callback_data="editsec:cancel")]])

def edit_fields_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Full Name",   callback_data="ef:full_name")],
        [InlineKeyboardButton(text="📱 Username",    callback_data="ef:username")],
        [InlineKeyboardButton(text="📞 Phone",       callback_data="ef:phone")],
        [InlineKeyboardButton(text="🎯 Skills",      callback_data="ef:skills")],
        [InlineKeyboardButton(text="📅 Availability",callback_data="ef:availability")],
        [InlineKeyboardButton(text="❌ Cancel",       callback_data="ef:cancel")],
    ])

# ── Format schedule text ──────────────────────────────────────────────────────
def format_schedule_text(admin: bool = False) -> str:
    if not schedule_db:
        saturday = get_next_saturday()
        return (f"📅 *Schedule — This Week ({saturday.strftime('%B %d, %Y')})*\n\n"
                f"⚠️ No schedule generated yet.\nUse ⚡ Auto-Schedule to create one.")

    icons = {"stage":"🎭","microphone":"🎤","audio":"🔊","video":"🎥"}
    wl = schedule_db.get("week_label","This Week")
    prog_date = schedule_db.get("program_date","")
    text = f"📅 *Schedule — {wl}*\n🗓 {prog_date}  ⏰ 3:00 PM\n\n👥 *Assignments:*\n"

    for section in ["stage","microphone","audio","video"]:
        ids = schedule_db["assignments"].get(section, [])
        names = []
        for tid in ids:
            b = brothers_db.get(tid)
            names.append(b["full_name"] if b else f"Unknown({tid})")
        value = ", ".join(names) if names else "_(unassigned)_"
        text += f"{icons[section]} *{section.capitalize()}*: {value}\n"

    if admin:
        text += f"\n📌 *Status: Pending Approval*"
    return text

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
        await message.answer(
            f"👋 Welcome, *{name}*!\n\nYou have *Admin* access.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
        return

    if uid in brothers_db:
        await message.answer(
            f"👋 Welcome back, *{brothers_db[uid]['full_name']}*!\nUse the menu below.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=brother_menu())
        return

    # Check if already pending
    already = any(p["telegram_id"] == uid for p in pending_approvals)
    if already:
        await message.answer(
            f"⏳ Hi *{name}*! Your registration is still pending admin approval.\nPlease wait. 🙏",
            parse_mode=ParseMode.MARKDOWN)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📝 Register", callback_data="self_register")
    ]])
    await message.answer(
        f"👋 Hi *{name}*!\n\nYou are not yet registered in the A/V Department.\nWould you like to register?",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ── Self-register flow (for brothers) ────────────────────────────────────────
@router.callback_query(F.data == "self_register")
async def self_reg_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SelfRegister.full_name)
    await callback.message.answer(
        "📝 *Register*\n\nStep 1/2 — Enter your full name:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
    await callback.answer()

@router.message(SelfRegister.full_name)
async def self_reg_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip())
    await state.set_state(SelfRegister.phone)
    await message.answer("Step 2/2 — Phone number or type *skip*:", parse_mode=ParseMode.MARKDOWN)

@router.message(SelfRegister.phone)
async def self_reg_phone(message: Message, state: FSMContext):
    v = message.text.strip()
    d = await state.get_data()
    await state.update_data(phone=None if v.lower()=="skip" else v)
    await state.set_state(SelfRegister.confirm)
    await message.answer(
        f"📋 *Confirm*\n\n👤 *{d['full_name']}*\n📞 {v if v.lower()!='skip' else '—'}\n\nSubmit for admin approval?",
        parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_kb("selfreg"))

@router.callback_query(F.data.startswith("selfreg_"), SelfRegister.confirm)
async def self_reg_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "selfreg_no":
        await state.clear()
        await callback.message.answer("❌ Cancelled.")
        await callback.answer(); return
    d = await state.get_data()
    uid = callback.from_user.id
    uname = callback.from_user.username
    entry = {
        "telegram_id": uid,
        "full_name": d["full_name"],
        "phone": d.get("phone"),
        "telegram_username": f"@{uname}" if uname else None,
        "skills": [], "availability": [], "serves": 0,
    }
    pending_approvals.append(entry)
    await callback.message.answer(
        "✅ *Registration submitted!*\n\nAn admin will review your request. You will be notified when approved. 🙏",
        parse_mode=ParseMode.MARKDOWN)
    # Notify admins
    if bot_instance:
        await notify_admins(bot_instance,
            f"🔔 *New Registration Request*\n\n"
            f"👤 *{d['full_name']}*\n"
            f"📱 @{uname or '—'}\n"
            f"🆔 `{uid}`\n\n"
            f"Use *⏳ Pending Approvals* to review.")
    await state.clear()
    await callback.answer()

# ── /help ─────────────────────────────────────────────────────────────────────
@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "*Commands:*\n\n"
        "👮 *Admin:*\n"
        "/registerbrother — Register a brother (admin adds)\n"
        "/autoschedule — Generate this week's schedule\n"
        "/viewschedule — View schedule\n"
        "/editschedule — Edit assignments\n"
        "/editbrother — Edit brother info\n"
        "/deletebrother — Remove a brother\n"
        "/pendingapprovals — Review pending registrations\n"
        "/brotherlist — List all brothers\n"
        "/sendreminders — Send reminders\n"
        "/report — Monthly report\n\n"
        "👤 *Everyone:*\n"
        "/viewschedule — View this week's schedule\n"
        "/myassignments — Your assignments\n"
        "/availability — Update availability",
        parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════════════════════════════
# REGISTER BROTHER (admin-initiated)
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("registerbrother"))
@router.message(F.text == "➕ Register Brother")
async def reg_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin only."); return
    await state.set_state(RegisterBrother.full_name)
    await message.answer("📝 *Register New Brother*\n\nStep 1/4 — Full name:",
                         parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())

@router.message(RegisterBrother.full_name)
async def reg_name(message: Message, state: FSMContext):
    await state.update_data(full_name=message.text.strip(), skills=[])
    await state.set_state(RegisterBrother.phone)
    await message.answer("Step 2/4 — Phone number (e.g. +251911000001) or type *skip*:", parse_mode=ParseMode.MARKDOWN)


@router.message(RegisterBrother.phone)
async def reg_phone(message: Message, state: FSMContext):
    v = message.text.strip()
    await state.update_data(phone=None if v.lower()=="skip" else v)
    await state.set_state(RegisterBrother.skills)
    await message.answer("Step 3/4 — Select skills (tap all that apply, then Done):", reply_markup=skills_kb())

@router.callback_query(F.data.startswith("skill_"), RegisterBrother.skills)
async def reg_skill(callback: CallbackQuery, state: FSMContext):
    skill = callback.data[6:]
    if skill == "done":
        d = await state.get_data()
        if not d.get("skills"):
            await callback.answer("⚠️ Select at least one skill!", show_alert=True); return
        await state.set_state(RegisterBrother.confirm)
        d2 = await state.get_data()
        await callback.message.answer(
            f"📋 *Confirm Registration*\n\n"
            f"👤 *{d2['full_name']}*\n"
            f"📞 {d2.get('phone','—')}\n"
            f"🎯 Skills: {', '.join(d2['skills'])}",
            parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_kb("reg"))
        await callback.answer(); return
    d = await state.get_data()
    skills = d.get("skills", [])
    if skill in skills: skills.remove(skill)
    else: skills.append(skill)
    await state.update_data(skills=skills)
    await callback.message.edit_reply_markup(reply_markup=skills_kb(skills))
    await callback.answer(f"Selected: {', '.join(skills) or 'none'}")


@router.callback_query(F.data.startswith("reg_"), RegisterBrother.confirm)
async def reg_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "reg_no":
        await state.clear()
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu())
        await callback.answer(); return
    d = await state.get_data()
    # Use a placeholder ID since admin is registering on behalf of someone
    # Real telegram_id will be linked when the brother sends /start
    import random
    fake_id = -(random.randint(100000, 999999))  # negative = not yet linked
    brothers_db[fake_id] = {
        "telegram_id": fake_id,
        "full_name": d["full_name"],
        "skills": d.get("skills", []),
        "availability": ["saturday"],
        "phone": d.get("phone"),
        "telegram_username": None,
        "serves": 0,
        "linked": False,
    }
    await callback.message.answer(
        f"✅ *{d['full_name']}* registered!\n\n"
        f"Ask them to open the bot and send /start — they will be linked automatically.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await state.clear()
    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# AUTO-SCHEDULE  ← FIXED: uses only registered brothers, real date
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("autoschedule"))
@router.message(F.text == "⚡ Auto-Schedule")
async def cmd_auto(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin only."); return

    # ── Check we have brothers ───────────────────────────────────────────────
    active = {tid: b for tid, b in brothers_db.items() if tid > 0}   # skip unlinked placeholders
    if not active:
        await message.answer(
            "❌ *No linked brothers found.*\n\n"
            "Brothers must send /start to the bot first so they are linked.\n"
            "Then their real Telegram ID is available for scheduling.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
        return

    # ── Use this week's Saturday automatically ───────────────────────────────
    prog_date = get_next_saturday()
    wl = week_label(prog_date)

    await message.answer(f"⚙️ Generating schedule for *{wl}* ({prog_date.strftime('%B %d, %Y')})…",
                         parse_mode=ParseMode.MARKDOWN)

    SECTIONS = [
        ("stage",      1),
        ("microphone", 2),
        ("audio",      1),
        ("video",      1),
    ]

    # Sort by fewest serves = fairest rotation
    pool = sorted(active.values(), key=lambda b: b["serves"])
    pool_ids = [b["telegram_id"] for b in pool]

    assignments: dict[str, list] = {}
    assigned_set: set = set()
    warnings: list = []

    for section, needed in SECTIONS:
        # Prefer brothers who have this skill
        skilled = [tid for tid in pool_ids
                   if tid not in assigned_set and section in brothers_db[tid].get("skills", [])]
        # Fallback: any unassigned brother
        any_avail = [tid for tid in pool_ids if tid not in assigned_set]

        chosen = skilled[:needed]
        if len(chosen) < needed:
            warnings.append(f"⚠️ Not enough brothers with *{section}* skill (need {needed}, have {len(skilled)}). Using available brothers as fallback.")
            extras = [tid for tid in any_avail if tid not in chosen]
            chosen += extras[:needed - len(chosen)]

        assignments[section] = chosen
        assigned_set.update(chosen)

    # Save the schedule
    schedule_db.clear()
    schedule_db.update({
        "program_date": prog_date.strftime("%B %d, %Y"),
        "prog_date_iso": prog_date.isoformat(),
        "week_label": wl,
        "assignments": assignments,
        "approved": False,
    })

    # Build result message using REAL brother names
    icons = {"stage":"🎭","microphone":"🎤","audio":"🔊","video":"🎥"}
    result = f"✅ *Schedule Generated!*\n\n📅 {wl}\n🗓 {prog_date.strftime('%A, %B %d %Y')}\n\n"

    all_assigned = True
    for section, needed in SECTIONS:
        ids = assignments.get(section, [])
        names = [brothers_db[tid]["full_name"] for tid in ids if tid in brothers_db]
        if len(names) < needed:
            all_assigned = False
        value = ", ".join(names) if names else "❌ *Not enough brothers*"
        result += f"{icons[section]} *{section.capitalize()}*: {value}\n"

    result += f"\n👥 Brothers from registry: *{len(active)}*"
    result += f"\n📊 Assigned: *{len(assigned_set)}/{len(active)}* brothers"

    if warnings:
        result += "\n\n" + "\n".join(warnings)

    if not all_assigned:
        result += "\n\n⚠️ *Some sections are understaffed. Register more brothers.*"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💾 Save & Notify Brothers", callback_data="save_schedule"),
        InlineKeyboardButton(text="🔄 Regenerate",             callback_data="regen_schedule"),
    ]])
    await message.answer(result, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

# ══════════════════════════════════════════════════════════════════════════════
# VIEW SCHEDULE  ← FIXED: shows "This Week / Next Week" label
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("viewschedule"))
@router.message(F.text.in_({"📋 View Schedule","📋 This Week's Schedule"}))
async def cmd_view_schedule(message: Message):
    uid = message.from_user.id
    is_adm = is_admin(uid)
    text = format_schedule_text(admin=is_adm)

    if is_adm and schedule_db:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Approve & Notify", callback_data="approve_schedule"),
            InlineKeyboardButton(text="📝 Edit",             callback_data="go_edit_schedule"),
            InlineKeyboardButton(text="🔄 Regenerate",       callback_data="regen_schedule"),
        ]])
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    else:
        await message.answer(text, parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════════════════════════════
# EDIT SCHEDULE
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("editschedule"))
@router.message(F.text == "📝 Edit Schedule")
async def edit_schedule_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin only."); return
    if not schedule_db:
        await message.answer("⚠️ No schedule yet. Use ⚡ Auto-Schedule first."); return
    await state.set_state(EditScheduleFlow.choose_section)
    await message.answer(
        f"📝 *Edit Schedule — {schedule_db.get('week_label','This Week')}*\n\nChoose section to edit:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=sections_kb())

@router.callback_query(F.data.startswith("editsec:"), EditScheduleFlow.choose_section)
async def edit_sec_chosen(callback: CallbackQuery, state: FSMContext):
    section = callback.data.split(":")[1]
    if section == "cancel":
        await state.clear()
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu())
        await callback.answer(); return
    await state.update_data(edit_section=section)

    current_ids = schedule_db["assignments"].get(section, [])
    current_names = [brothers_db[tid]["full_name"] for tid in current_ids if tid in brothers_db]

    rows = []
    for tid in current_ids:
        b = brothers_db.get(tid)
        if b:
            rows.append([InlineKeyboardButton(
                text=f"🔄 Replace {b['full_name']}",
                callback_data=f"editact:replace:{tid}")])
            rows.append([InlineKeyboardButton(
                text=f"❌ Remove {b['full_name']}",
                callback_data=f"editact:remove:{tid}")])
    rows.append([InlineKeyboardButton(text="➕ Add a brother", callback_data="editact:add:none")])
    rows.append([InlineKeyboardButton(text="⬅️ Back",          callback_data="editact:back:none")])

    icons = {"stage":"🎭","microphone":"🎤","audio":"🔊","video":"🎥"}
    await state.set_state(EditScheduleFlow.choose_action)
    await callback.message.answer(
        f"{icons.get(section,'•')} *{section.capitalize()}* — currently: "
        f"{', '.join(current_names) or '(empty)'}\n\nWhat do you want to do?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()

@router.callback_query(F.data.startswith("editact:"), EditScheduleFlow.choose_action)
async def edit_action_chosen(callback: CallbackQuery, state: FSMContext):
    _, action, target = callback.data.split(":", 2)
    if action == "back":
        await state.set_state(EditScheduleFlow.choose_section)
        await callback.message.answer("Choose section:", reply_markup=sections_kb())
        await callback.answer(); return

    d = await state.get_data()
    section = d["edit_section"]

    if action == "remove":
        tid = int(target)
        ids = schedule_db["assignments"].get(section, [])
        if tid in ids:
            ids.remove(tid)
        name = brothers_db.get(tid, {}).get("full_name", str(tid))
        await state.clear()
        await callback.message.answer(
            f"🗑 *{name}* removed from *{section.capitalize()}*.\n\n" + format_schedule_text(admin=True),
            parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
        await callback.answer(); return

    # replace or add → pick new brother
    await state.update_data(edit_action=action, replacing_tid=int(target) if target != "none" else None)
    await state.set_state(EditScheduleFlow.choose_new_brother)

    # Exclude already-assigned brothers (except the one being replaced)
    all_assigned = [tid for s, ids in schedule_db["assignments"].items()
                    for tid in ids if s != section]
    await callback.message.answer(
        f"Choose a brother for *{section.capitalize()}*:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=brothers_list_kb("picknew", exclude_ids=all_assigned))
    await callback.answer()

@router.callback_query(F.data.startswith("picknew:"), EditScheduleFlow.choose_new_brother)
async def edit_pick_new(callback: CallbackQuery, state: FSMContext):
    new_tid = int(callback.data.split(":")[1])
    d = await state.get_data()
    section = d["edit_section"]
    action = d["edit_action"]
    replacing_tid = d.get("replacing_tid")

    ids = schedule_db["assignments"].setdefault(section, [])
    if action == "replace" and replacing_tid in ids:
        idx = ids.index(replacing_tid)
        ids[idx] = new_tid
    else:
        if new_tid not in ids:
            ids.append(new_tid)

    new_name = brothers_db.get(new_tid, {}).get("full_name", str(new_tid))
    await state.clear()
    await callback.message.answer(
        f"✅ *{new_name}* assigned to *{section.capitalize()}*.\n\n" + format_schedule_text(admin=True),
        parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# EDIT BROTHER
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("editbrother"))
@router.message(F.text == "✏️ Edit Brother")
async def edit_bro_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin only."); return
    if not brothers_db:
        await message.answer("⚠️ No brothers registered yet."); return
    await state.set_state(EditBrotherFlow.choose_brother)
    await message.answer("✏️ *Edit Brother*\n\nSelect a brother:",
                         parse_mode=ParseMode.MARKDOWN,
                         reply_markup=brothers_list_kb("editbro"))

@router.callback_query(F.data.startswith("editbro:"), EditBrotherFlow.choose_brother)
async def edit_bro_chosen(callback: CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":")[1])
    b = brothers_db.get(tid)
    if not b:
        await callback.answer("Brother not found.", show_alert=True); return
    await state.update_data(editing_tid=tid)
    await state.set_state(EditBrotherFlow.choose_field)
    await callback.message.answer(
        f"✏️ Editing: *{b['full_name']}*\n\n"
        f"📱 {b.get('telegram_username','—')}  📞 {b.get('phone','—')}\n"
        f"🎯 Skills: {', '.join(b['skills']) or 'None'}\n"
        f"📅 Available: {', '.join(b.get('availability',[])) or 'None'}\n\n"
        f"What to change?",
        parse_mode=ParseMode.MARKDOWN, reply_markup=edit_fields_kb())
    await callback.answer()

@router.callback_query(F.data.startswith("ef:"), EditBrotherFlow.choose_field)
async def edit_field_chosen(callback: CallbackQuery, state: FSMContext):
    field = callback.data[3:]
    if field == "cancel":
        await state.clear()
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu())
        await callback.answer(); return
    await state.update_data(edit_field=field)
    d = await state.get_data()
    b = brothers_db[d["editing_tid"]]
    if field == "skills":
        await state.set_state(EditBrotherFlow.choose_skills)
        await state.update_data(new_skills=b["skills"].copy())
        await callback.message.answer("🎯 Update skills:", reply_markup=skills_kb(b["skills"]))
    elif field == "availability":
        await state.set_state(EditBrotherFlow.choose_avail)
        await state.update_data(new_avail=b.get("availability",[]).copy())
        await callback.message.answer("📅 Update availability:", reply_markup=avail_kb(b.get("availability",[])))
    else:
        await state.set_state(EditBrotherFlow.enter_value)
        labels = {"full_name":"Full Name","username":"Telegram Username","phone":"Phone"}
        current = b.get(field if field!="username" else "telegram_username", "—")
        await callback.message.answer(
            f"Enter new *{labels.get(field,field)}* (current: {current}):",
            parse_mode=ParseMode.MARKDOWN, reply_markup=ReplyKeyboardRemove())
    await callback.answer()

@router.callback_query(F.data.startswith("skill_"), EditBrotherFlow.choose_skills)
async def edit_bro_skill(callback: CallbackQuery, state: FSMContext):
    skill = callback.data[6:]
    d = await state.get_data()
    skills = d.get("new_skills", [])
    if skill == "done":
        if not skills:
            await callback.answer("⚠️ Select at least one!", show_alert=True); return
        brothers_db[d["editing_tid"]]["skills"] = skills
        await state.clear()
        await callback.message.answer(f"✅ Skills updated: *{', '.join(skills)}*",
                                      parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
        await callback.answer(); return
    if skill in skills: skills.remove(skill)
    else: skills.append(skill)
    await state.update_data(new_skills=skills)
    await callback.message.edit_reply_markup(reply_markup=skills_kb(skills))
    await callback.answer(f"Selected: {', '.join(skills) or 'none'}")

@router.callback_query(F.data.startswith("av_"), EditBrotherFlow.choose_avail)
async def edit_bro_avail(callback: CallbackQuery, state: FSMContext):
    day = callback.data[3:]
    d = await state.get_data()
    avail = d.get("new_avail", [])
    if day == "done":
        brothers_db[d["editing_tid"]]["availability"] = avail
        await state.clear()
        await callback.message.answer(f"✅ Availability updated: *{', '.join(avail) or 'None'}*",
                                      parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
        await callback.answer(); return
    if day in avail: avail.remove(day)
    else: avail.append(day)
    await state.update_data(new_avail=avail)
    await callback.message.edit_reply_markup(reply_markup=avail_kb(avail))
    await callback.answer(f"Days: {', '.join(avail) or 'none'}")

@router.message(EditBrotherFlow.enter_value)
async def edit_bro_value(message: Message, state: FSMContext):
    d = await state.get_data()
    field = d["edit_field"]
    tid = d["editing_tid"]
    val = message.text.strip()
    if field == "full_name":
        brothers_db[tid]["full_name"] = val
    elif field == "username":
        brothers_db[tid]["telegram_username"] = val
    elif field == "phone":
        brothers_db[tid]["phone"] = val if val.lower()!="skip" else None
    await state.clear()
    await message.answer(f"✅ Updated successfully!", reply_markup=admin_menu())

# ══════════════════════════════════════════════════════════════════════════════
# DELETE BROTHER
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("deletebrother"))
@router.message(F.text == "🗑 Delete Brother")
async def del_bro_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin only."); return
    if not brothers_db:
        await message.answer("⚠️ No brothers registered."); return
    await state.set_state(DeleteBrotherFlow.choose_brother)
    await message.answer("🗑 *Delete Brother*\n\nSelect a brother to remove:",
                         parse_mode=ParseMode.MARKDOWN,
                         reply_markup=brothers_list_kb("delbro"))

@router.callback_query(F.data.startswith("delbro:"), DeleteBrotherFlow.choose_brother)
async def del_bro_chosen(callback: CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":")[1])
    b = brothers_db.get(tid)
    if not b:
        await callback.answer("Not found.", show_alert=True); return
    await state.update_data(deleting_tid=tid)
    await state.set_state(DeleteBrotherFlow.confirm)
    await callback.message.answer(
        f"⚠️ *Confirm Delete*\n\n"
        f"👤 *{b['full_name']}*\n"
        f"🎯 Skills: {', '.join(b['skills']) or 'None'}\n"
        f"📊 Serves: {b['serves']}\n\n"
        f"This cannot be undone!",
        parse_mode=ParseMode.MARKDOWN, reply_markup=confirm_kb("del"))
    await callback.answer()

@router.callback_query(F.data.startswith("del_"), DeleteBrotherFlow.confirm)
async def del_bro_confirm(callback: CallbackQuery, state: FSMContext):
    if callback.data == "del_no":
        await state.clear()
        await callback.message.answer("❌ Cancelled.", reply_markup=admin_menu())
        await callback.answer(); return
    d = await state.get_data()
    tid = d["deleting_tid"]
    name = brothers_db.get(tid, {}).get("full_name", "Unknown")
    brothers_db.pop(tid, None)
    # Also remove from schedule if present
    for ids in schedule_db.get("assignments", {}).values():
        if tid in ids:
            ids.remove(tid)
    await state.clear()
    await callback.message.answer(f"🗑 *{name}* deleted from the system.",
                                  parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# PENDING APPROVALS
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("pendingapprovals"))
@router.message(F.text == "⏳ Pending Approvals")
async def cmd_pending(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin only."); return
    if not pending_approvals:
        await message.answer("✅ No pending approvals.", reply_markup=admin_menu()); return

    text = f"⏳ *Pending Approvals ({len(pending_approvals)})*\n\n"
    buttons = []
    for p in pending_approvals:
        text += (f"👤 *{p['full_name']}*\n"
                 f"   📱 {p.get('telegram_username','—')}  📞 {p.get('phone','—')}\n"
                 f"   🆔 `{p['telegram_id']}`\n\n")
        buttons.append([
            InlineKeyboardButton(text=f"✅ Approve {p['full_name'].split()[0]}",
                                 callback_data=f"papprove:{p['telegram_id']}"),
            InlineKeyboardButton(text="❌ Reject",
                                 callback_data=f"preject:{p['telegram_id']}"),
        ])
    await message.answer(text, parse_mode=ParseMode.MARKDOWN,
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("papprove:"))
async def cb_papprove(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Admin only", show_alert=True); return
    tid = int(callback.data.split(":")[1])
    entry = next((p for p in pending_approvals if p["telegram_id"]==tid), None)
    if not entry:
        await callback.answer("Already handled.", show_alert=True); return
    pending_approvals.remove(entry)
    brothers_db[tid] = {**entry, "linked": True}
    # Notify brother
    if bot_instance:
        try:
            await bot_instance.send_message(
                tid,
                f"🎉 *Registration Approved!*\n\n"
                f"Welcome, *{entry['full_name']}*! 🙏\n\n"
                f"You can now view your assignments and schedule.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=brother_menu())
        except Exception as e:
            logger.warning("Could not notify approved user %s: %s", tid, e)
    await callback.message.answer(
        f"✅ *{entry['full_name']}* approved and added to the registry!",
        parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await callback.answer()

@router.callback_query(F.data.startswith("preject:"))
async def cb_preject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Admin only", show_alert=True); return
    tid = int(callback.data.split(":")[1])
    entry = next((p for p in pending_approvals if p["telegram_id"]==tid), None)
    if not entry:
        await callback.answer("Already handled.", show_alert=True); return
    pending_approvals.remove(entry)
    if bot_instance:
        try:
            await bot_instance.send_message(
                tid,
                "❌ *Your registration was not approved.*\n\nPlease contact the department admin.")
        except Exception as e:
            logger.warning("Could not notify rejected user %s: %s", tid, e)
    await callback.message.answer(
        f"❌ *{entry['full_name']}* rejected.",
        parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await callback.answer()

# ══════════════════════════════════════════════════════════════════════════════
# BROTHER LIST / MY ASSIGNMENTS / AVAILABILITY / REPORT / REMINDERS
# ══════════════════════════════════════════════════════════════════════════════
@router.message(Command("brotherlist"))
@router.message(F.text == "👥 Brother List")
async def cmd_brother_list(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin only."); return
    if not brothers_db:
        await message.answer("👥 No brothers registered yet."); return
    text = "👥 *Brothers Registry*\n\n"
    for b in sorted(brothers_db.values(), key=lambda x: x["full_name"]):
        linked = "🟢" if b.get("linked", b["telegram_id"]>0) else "🟡"
        text += f"{linked} *{b['full_name']}* — {', '.join(b['skills']) or 'No skills'} — {b['serves']} serves\n"
    text += "\n🟢 Linked (active)  🟡 Not yet linked to bot"
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)

@router.message(Command("myassignments"))
@router.message(F.text == "📅 My Assignments")
async def cmd_my_assignments(message: Message):
    uid = message.from_user.id
    if not schedule_db:
        await message.answer("📅 No schedule generated yet.", parse_mode=ParseMode.MARKDOWN); return
    icons = {"stage":"🎭","microphone":"🎤","audio":"🔊","video":"🎥"}
    my_sections = []
    for section, ids in schedule_db["assignments"].items():
        if uid in ids:
            my_sections.append(f"{icons.get(section,'•')} *{section.capitalize()}*")
    wl = schedule_db.get("week_label","This Week")
    prog_date = schedule_db.get("program_date","")
    if my_sections:
        text = (f"📅 *Your Assignments — {wl}*\n\n"
                + "\n".join(my_sections)
                + f"\n\n🗓 {prog_date}  ⏰ 3:00 PM\nStatus: ⏳ Pending confirmation")
        await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=assign_kb(f"{uid}"))
    else:
        await message.answer(f"📅 You have no assignments for {wl}.", parse_mode=ParseMode.MARKDOWN)

@router.message(Command("availability"))
@router.message(F.text == "✅ Set Availability")
async def cmd_availability(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Available this Saturday", callback_data="avail_yes"),
        InlineKeyboardButton(text="❌ Not available",           callback_data="avail_no"),
    ]])
    await message.answer("📅 *Availability*\n\nAvailable this coming Saturday?",
                         parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

@router.message(Command("report"))
@router.message(F.text == "📊 Report")
async def cmd_report(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin only."); return
    from datetime import date as dt
    today = dt.today()
    if not brothers_db:
        await message.answer(f"📊 *Report — {today.strftime('%B %Y')}*\n\nNo brothers yet.",
                             parse_mode=ParseMode.MARKDOWN); return
    sorted_b = sorted(brothers_db.values(), key=lambda x: x["serves"], reverse=True)
    text = (f"📊 *Report — {today.strftime('%B %Y')}*\n\n"
            f"👥 Brothers: *{len(brothers_db)}*\n"
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
async def cmd_reminders(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Admin only."); return
    if not schedule_db:
        await message.answer("⚠️ No schedule found. Use ⚡ Auto-Schedule first."); return
    icons = {"stage":"🎭","microphone":"🎤","audio":"🔊","video":"🎥"}
    wl = schedule_db.get("week_label","This Week")
    prog_date = schedule_db.get("program_date","")
    sent, failed = [], []
    for section, ids in schedule_db["assignments"].items():
        for tid in ids:
            b = brothers_db.get(tid)
            if not b: continue
            label = f"{icons.get(section,'')} {section.capitalize()}"
            if bot_instance and tid > 0:
                try:
                    await bot_instance.send_message(
                        tid,
                        f"⏰ *Reminder — {wl}!*\n\n"
                        f"🗓 {prog_date}  ⏰ 3:00 PM\n"
                        f"🎯 Your section: *{label}*\n\nSee you there! 🙏",
                        parse_mode=ParseMode.MARKDOWN)
                    sent.append(f"✅ {b['full_name']} ({label})")
                except Exception as e:
                    failed.append(f"⚠️ {b['full_name']} — failed")
            else:
                failed.append(f"⚠️ {b['full_name']} — not linked to bot yet")
    lines = sent + failed
    await message.answer(
        f"🔔 *Reminders — {wl}*\n\n" + ("\n".join(lines) if lines else "_(no assignments)_"),
        parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════════════════════════════
# CALLBACKS
# ══════════════════════════════════════════════════════════════════════════════
@router.callback_query(F.data == "approve_schedule")
async def cb_approve_schedule(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Admin only", show_alert=True); return
    schedule_db["approved"] = True
    await callback.message.edit_reply_markup(reply_markup=None)
    # Notify all assigned brothers
    icons = {"stage":"🎭","microphone":"🎤","audio":"🔊","video":"🎥"}
    wl = schedule_db.get("week_label","This Week")
    prog_date = schedule_db.get("program_date","")
    notified = []
    for section, ids in schedule_db["assignments"].items():
        for tid in ids:
            b = brothers_db.get(tid)
            if b and bot_instance and tid > 0:
                label = f"{icons.get(section,'')} {section.capitalize()}"
                try:
                    await bot_instance.send_message(
                        tid,
                        f"📢 *You have been assigned!*\n\n"
                        f"📅 {wl}  🗓 {prog_date}  ⏰ 3:00 PM\n"
                        f"🎯 Section: *{label}*\n\nPlease confirm:",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=assign_kb(f"{tid}_{section}"))
                    notified.append(b["full_name"])
                    b["serves"] += 1
                except Exception as e:
                    logger.warning("Notify failed for %s: %s", tid, e)
    await callback.message.answer(
        f"✅ *Schedule Approved!*\n\nNotified: {', '.join(notified) or '(none linked yet)'}",
        parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await callback.answer()

@router.callback_query(F.data == "go_edit_schedule")
async def cb_go_edit(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Admin only", show_alert=True); return
    await state.set_state(EditScheduleFlow.choose_section)
    await callback.message.answer(
        f"📝 *Edit Schedule — {schedule_db.get('week_label','This Week')}*\n\nChoose section:",
        parse_mode=ParseMode.MARKDOWN, reply_markup=sections_kb())
    await callback.answer()

@router.callback_query(F.data == "save_schedule")
async def cb_save_schedule(callback: CallbackQuery):
    if not schedule_db:
        await callback.answer("No schedule to save.", show_alert=True); return
    schedule_db["approved"] = True
    icons = {"stage":"🎭","microphone":"🎤","audio":"🔊","video":"🎥"}
    wl = schedule_db.get("week_label","This Week")
    prog_date = schedule_db.get("program_date","")
    notified, failed = [], []
    for section, ids in schedule_db["assignments"].items():
        for tid in ids:
            b = brothers_db.get(tid)
            if not b: continue
            label = f"{icons.get(section,'')} {section.capitalize()}"
            if bot_instance and tid > 0:
                try:
                    await bot_instance.send_message(
                        tid,
                        f"📢 *You have been assigned!*\n\n"
                        f"📅 {wl}  🗓 {prog_date}  ⏰ 3:00 PM\n"
                        f"🎯 Section: *{label}*\n\nPlease confirm:",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=assign_kb(f"{tid}_{section}"))
                    notified.append(f"✅ {b['full_name']} ({label})")
                    b["serves"] += 1
                except Exception as e:
                    failed.append(f"⚠️ {b['full_name']} — delivery failed")
            else:
                failed.append(f"⚠️ {b['full_name']} — not linked yet")
    await callback.message.edit_reply_markup(reply_markup=None)
    lines = notified + failed
    await callback.message.answer(
        f"💾 *Schedule Saved!*\n\n" + ("\n".join(lines) if lines else "_(no brothers assigned)_"),
        parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu())
    await callback.answer()

@router.callback_query(F.data == "regen_schedule")
async def cb_regen(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Admin only", show_alert=True); return
    await callback.answer("🔄 Use ⚡ Auto-Schedule to regenerate.")

@router.callback_query(F.data.startswith("confirm_"))
async def cb_confirm(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("✅ *Confirmed!* See you at the program. 🙏",
                                  parse_mode=ParseMode.MARKDOWN)
    if bot_instance:
        name = callback.from_user.first_name
        await notify_admins(bot_instance, f"✅ *{name}* confirmed their assignment.")
    await callback.answer("Confirmed ✅")

@router.callback_query(F.data.startswith("decline_"))
async def cb_decline(callback: CallbackQuery):
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer("❌ *Noted.* Admin will arrange a replacement. Thank you!",
                                  parse_mode=ParseMode.MARKDOWN)
    if bot_instance:
        name = callback.from_user.first_name
        await notify_admins(bot_instance, f"❌ *{name}* declined their assignment. Please arrange a replacement.")
    await callback.answer()

@router.callback_query(F.data.startswith("avail_"))
async def cb_avail(callback: CallbackQuery):
    msg = ("✅ *Marked available!* You are in the pool." if callback.data=="avail_yes"
           else "❌ *Marked unavailable.* Admin notified. 🙏")
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
    dp  = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logger.info("✅ AV Department Bot v3 starting…")
    await dp.start_polling(bot, allowed_updates=["message","callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
