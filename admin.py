import html
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import config
from queue_manager import QueueManager


def _require_admin(user_id: int) -> bool:
    return config.is_admin(user_id)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats - Display bot statistics and configuration (Admin only)."""
    user = update.effective_user
    if not user or not _require_admin(user.id):
        await update.effective_message.reply_text("⛔ This command is restricted to administrators.")
        return

    qm: QueueManager = context.application.bot_data["queue_manager"]
    active_count = len(qm.active_user_jobs)
    queued_count = qm.queue.qsize()

    text = (
        "📊 <b>Bot Statistics & Configuration</b>\n\n"
        f"• <b>Active User Jobs:</b> {active_count}\n"
        f"• <b>Jobs Waiting in Queue:</b> {queued_count}\n"
        f"• <b>Max Concurrent Workers:</b> {config.max_concurrent_jobs}\n"
        f"• <b>Total Jobs Processed:</b> {config.total_jobs_processed}\n"
        f"• <b>Total Videos Uploaded:</b> {config.total_videos_uploaded}\n"
        f"• <b>Total Failed URLs:</b> {config.total_Download_failures}\n\n"
        "⚙️ <b>Current Limits:</b>\n"
        f"• <b>Private Mode:</b> {'Enabled 🔒' if config.private_mode else 'Disabled 🌐'}\n"
        f"• <b>Max URLs per TXT:</b> {config.max_urls}\n"
        f"• <b>Max Upload Size:</b> {config.max_video_mb} MB\n"
        f"• <b>Authorized Users:</b> {len(config.authorized_users)}"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def jobs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/jobs - List all active and queued jobs (Admin only)."""
    user = update.effective_user
    if not user or not _require_admin(user.id):
        await update.effective_message.reply_text("⛔ This command is restricted to administrators.")
        return

    qm: QueueManager = context.application.bot_data["queue_manager"]
    if not qm.active_user_jobs:
        await update.effective_message.reply_text("✅ There are currently no active or queued jobs.")
        return

    lines = ["🔄 <b>Active & Queued Jobs:</b>\n"]
    for job in qm.active_user_jobs.values():
        lines.append(
            f"• <b>Job ID:</b> <code>{job.job_id}</code> | "
            f"<b>User:</b> <code>{job.user_id}</code> ({html.escape(job.username)})\n"
            f"  <b>State:</b> {job.state} | "
            f"<b>Progress:</b> {job.current_index}/{job.total_urls} "
            f"(✅ {job.completed_count} | ❌ {job.failed_count})"
        )

    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def setmaxurls_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setmaxurls <number> - Dynamically update MAX_URLS limit (Admin only)."""
    user = update.effective_user
    if not user or not _require_admin(user.id):
        await update.effective_message.reply_text("⛔ This command is restricted to administrators.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Usage: <code>/setmaxurls 20</code>", parse_mode=ParseMode.HTML
        )
        return

    new_limit = int(context.args[0])
    if new_limit < 1 or new_limit > 200:
        await update.effective_message.reply_text("⚠️ Please specify a number between 1 and 200.")
        return

    config.max_urls = new_limit
    await update.effective_message.reply_text(
        f"✅ Maximum URLs per TXT file updated to <b>{config.max_urls}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def setcaption_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setcaption <template> - Customize the video caption template (Admin only)."""
    user = update.effective_user
    if not user or not _require_admin(user.id):
        await update.effective_message.reply_text("⛔ This command is restricted to administrators.")
        return

    raw_text = update.effective_message.text or ""
    parts = raw_text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        help_msg = (
            "📝 <b>Custom Caption Configuration</b>\n\n"
            "Available placeholders:\n"
            "• <code>{title}</code> - Video Title\n"
            "• <code>{duration}</code> - Video Duration\n"
            "• <code>{size}</code> - File Size\n\n"
            "<b>Example usage:</b>\n"
            "<code>/setcaption Video Name: {title}\nDuration: {duration}\nFile Size: {size}\nProcessed By: TXT Leech Bot</code>"
        )
        await update.effective_message.reply_text(help_msg, parse_mode=ParseMode.HTML)
        return

    config.caption_template = parts[1].strip()
    await update.effective_message.reply_text(
        "✅ Custom caption template has been updated!\n\n"
        f"<b>Current Template:</b>\n<pre>{html.escape(config.caption_template)}</pre>",
        parse_mode=ParseMode.HTML,
    )


async def allow_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/allow <user_id> - Authorize a user when PRIVATE_MODE is enabled (Admin only)."""
    user = update.effective_user
    if not user or not _require_admin(user.id):
        await update.effective_message.reply_text("⛔ This command is restricted to administrators.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Usage: <code>/allow 123456789</code>", parse_mode=ParseMode.HTML
        )
        return

    target_id = int(context.args[0])
    config.authorized_users.add(target_id)
    await update.effective_message.reply_text(
        f"✅ User <code>{target_id}</code> is now authorized to use the bot.",
        parse_mode=ParseMode.HTML,
    )


async def deny_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/deny <user_id> - Revoke a user's access (Admin only)."""
    user = update.effective_user
    if not user or not _require_admin(user.id):
        await update.effective_message.reply_text("⛔ This command is restricted to administrators.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Usage: <code>/deny 123456789</code>", parse_mode=ParseMode.HTML
        )
        return

    target_id = int(context.args[0])
    if target_id in config.admin_ids:
        await update.effective_message.reply_text("⚠️ Cannot revoke access from an administrator configured in ADMIN_IDS.")
        return

    config.authorized_users.discard(target_id)
    await update.effective_message.reply_text(
        f"🚫 User <code>{target_id}</code> has been removed from authorized users.",
        parse_mode=ParseMode.HTML,
    )


async def private_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/private <on|off> - Toggle Private Mode at runtime (Admin only)."""
    user = update.effective_user
    if not user or not _require_admin(user.id):
        await update.effective_message.reply_text("⛔ This command is restricted to administrators.")
        return

    if not context.args or context.args[0].lower() not in ("on", "off", "true", "false"):
        await update.effective_message.reply_text(
            f"Current Private Mode: <b>{'ON' if config.private_mode else 'OFF'}</b>\n"
            "Usage: <code>/private on</code> or <code>/private off</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    arg = context.args[0].lower()
    config.private_mode = arg in ("on", "true")
    state_str = "ENABLED 🔒 (Authorized users only)" if config.private_mode else "DISABLED 🌐 (Public)"
    await update.effective_message.reply_text(
        f"✅ Private Mode is now <b>{state_str}</b>.",
        parse_mode=ParseMode.HTML,
    )

