import asyncio
import html
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Dict


from telegram import Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from admin import (
    allow_user_command,
    deny_user_command,
    jobs_command,
    private_mode_command,
    setcaption_command,
    setmaxurls_command,
    stats_command,
)
from config import config
from downloader import DownloadError, download_video_async, parse_and_clean_txt
from progress import (
    DownloadCancelledError,
    ProgressTracker,
    format_bytes,
    format_duration,
)
from queue_manager import Job, QueueManager

# Configure non-buffered structured logging for Render
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=logging.INFO,
)
# Silence overly verbose httpx logs so bot tokens never leak in URL debug logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("txt_leech_bot")


class SafeFormatDict(Dict[str, str]):
    """Prevents KeyError if an admin defines an unknown placeholder in caption_template."""
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_caption(title: str, duration_sec: int, size_bytes: int) -> str:
    """Format the video caption using the configurable template safely."""
    safe_values = SafeFormatDict(
        title=html.escape(title[:200]),
        duration=html.escape(format_duration(duration_sec)),
        size=html.escape(format_bytes(size_bytes)),
    )
    rendered = config.caption_template.format_map(safe_values)
    # Telegram captions have a strict 1024-character limit
    return rendered[:1024]


# ============================================================================
# User Commands
# ============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start - Welcome the user and explain basic usage."""
    user = update.effective_user
    if not user:
        return

    if not config.is_authorized(user.id):
        await update.effective_message.reply_text(
            "🔒 <b>Private Mode Enabled</b>\n"
            "You are not authorized to use this bot. Send <code>/id</code> to view your ID and ask the administrator for access.",
            parse_mode=ParseMode.HTML,
        )
        return

    text = (
        f"👋 <b>Welcome, {html.escape(user.first_name)}!</b>\n\n"
        "I am an <b>Advanced TXT-to-Video Leech Bot</b>.\n\n"
        "📄 <b>How to use me:</b>\n"
        "1. Create a <code>.txt</code> file containing one video URL per line.\n"
        "2. Send the <code>.txt</code> file directly to this chat.\n"
        "3. I will validate the links, download the videos sequentially with real-time progress, generate thumbnails, and upload the MP4 files back to you.\n\n"
        "⚠️ <i>Only submit video URLs that you are authorized to download and redistribute.</i>\n\n"
        "Use /help to see all commands and current limits."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help - Detailed instructions and commands."""
    user = update.effective_user
    is_admin = bool(user and config.is_admin(user.id))

    text = (
        "📖 <b>Usage Instructions & Commands</b>\n\n"
        "<b>TXT File Format Rules:</b>\n"
        "• Upload a standard UTF-8 <code>.txt</code> document.\n"
        "• Place one <code>http://</code> or <code>https://</code> URL per line.\n"
        "• Blank lines and lines starting with <code>#</code> are ignored.\n"
        "• Duplicate URLs are automatically removed.\n\n"
        "<b>Current Bot Limits:</b>\n"
        f"• Max URLs per TXT file: <b>{config.max_urls}</b>\n"
        f"• Max Video Upload Size: <b>{config.max_video_mb} MB</b>\n\n"
        "<b>User Commands:</b>\n"
        "/start - Start the bot\n"
        "/help - Show this help message\n"
        "/status - Check your active job progress\n"
        "/cancel - Cancel your active job\n"
        "/id - Get your Telegram User ID\n"
        "/about - About this bot"
    )

    if is_admin:
        text += (
            "\n\n👑 <b>Admin Commands:</b>\n"
            "/stats - View bot metrics & limits\n"
            "/jobs - View active and queued jobs\n"
            "/setmaxurls <code>&lt;n&gt;</code> - Change max URLs per TXT\n"
            "/setcaption <code>&lt;template&gt;</code> - Set custom caption\n"
            "/allow <code>&lt;user_id&gt;</code> - Authorize user\n"
            "/deny <code>&lt;user_id&gt;</code> - Revoke user access\n"
            "/private <code>&lt;on|off&gt;</code> - Toggle private mode"
        )

    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/about - Bot technology information."""
    text = (
        "ℹ️ <b>About TXT-to-Video Leech Bot</b>\n\n"
        "• <b>Runtime:</b> Python 3.12 (Asyncio)\n"
        "• <b>Framework:</b> python-telegram-bot v22.x\n"
        "• <b>Downloader Engine:</b> yt-dlp + FFmpeg/FFprobe\n"
        "• <b>Deployment:</b> Docker on Render Background Worker\n\n"
        "Designed for safe, sequential processing of authorized media links with automatic temporary storage cleanup."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/id - Display the user's Telegram ID and Chat ID."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    await update.effective_message.reply_text(
        f"🆔 <b>Your Telegram User ID:</b> <code>{user.id}</code>\n"
        f"💬 <b>Current Chat ID:</b> <code>{chat.id}</code>",
        parse_mode=ParseMode.HTML,
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status - Show the user's current job status."""
    user = update.effective_user
    if not user:
        return

    qm: QueueManager = context.application.bot_data["queue_manager"]
    job = qm.get_user_job(user.id)
    if not job:
        await update.effective_message.reply_text("ℹ️ You do not have any active or queued jobs right now.")
        return

    text = (
        f"📊 <b>Your Job Status (ID: <code>{job.job_id}</code>)</b>\n\n"
        f"• <b>State:</b> {job.state.upper()}\n"
        f"• <b>Current URL:</b> {job.current_index}/{job.total_urls}\n"
        f"• <b>Completed:</b> {job.completed_count}\n"
        f"• <b>Failed:</b> {job.failed_count}"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel - Cancel the user's currently running or queued job."""
    user = update.effective_user
    if not user:
        return

    qm: QueueManager = context.application.bot_data["queue_manager"]
    job = qm.cancel_user_job(user.id)
    if not job:
        await update.effective_message.reply_text("ℹ️ You do not have an active job to cancel.")
        return

    await update.effective_message.reply_text(
        f"🛑 <b>Cancellation signal sent for Job <code>{job.job_id}</code>.</b>\n"
        "Stopping active download and cleaning up temporary files...",
        parse_mode=ParseMode.HTML,
    )


# ============================================================================
# TXT Document Handler & Worker Processing
# ============================================================================

async def handle_txt_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Validate incoming .txt files, parse URLs, and enqueue a job."""
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat
    if not user or not message or not chat or not message.document:
        return

    if not config.is_authorized(user.id):
        await message.reply_text(
            "🔒 You are not authorized to use this bot. Use /id to get your ID.",
            parse_mode=ParseMode.HTML,
        )
        return

    qm: QueueManager = context.application.bot_data["queue_manager"]
    if qm.has_active_job(user.id):
        await message.reply_text(
            "⚠️ You already have an active job in progress or in the queue.\n"
            "Use /status to check it or /cancel to stop it before submitting a new file."
        )
        return

    doc = message.document
    file_name = (doc.file_name or "").lower()
    if not file_name.endswith(".txt"):
        await message.reply_text("⚠️ Please send a valid <code>.txt</code> file.", parse_mode=ParseMode.HTML)
        return

    if doc.file_size and doc.file_size > 2 * 1024 * 1024:
        await message.reply_text("⚠️ The <code>.txt</code> file is too large (maximum 2 MB).", parse_mode=ParseMode.HTML)
        return

    status_msg = await message.reply_text("🔍 Reading and validating your <code>.txt</code> file...", parse_mode=ParseMode.HTML)

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        raw_bytes = await tg_file.download_as_bytearray()
        content = raw_bytes.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.error("Failed to download .txt file from Telegram: %s", exc)
        await status_msg.edit_text("❌ Failed to download the <code>.txt</code> file from Telegram. Please try again.", parse_mode=ParseMode.HTML)
        return

    if not content.strip():
        await status_msg.edit_text("⚠️ Your <code>.txt</code> file is empty.", parse_mode=ParseMode.HTML)
        return

    valid_urls, duplicate_count = parse_and_clean_txt(content)
    if not valid_urls:
        await status_msg.edit_text(
            "⚠️ No valid <code>http://</code> or <code>https://</code> URLs were found in your file.\n"
            "Make sure each link is on its own line and does not start with <code>#</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    truncated_note = ""
    if len(valid_urls) > config.max_urls:
        valid_urls = valid_urls[: config.max_urls]
        truncated_note = f"\n⚠️ <i>Note: Truncated to the first {config.max_urls} URLs (configured limit).</i>"

    dup_note = f"\n♻️ <i>Removed {duplicate_count} duplicate URL(s).</i>" if duplicate_count > 0 else ""

    await status_msg.edit_text(
        f"✅ <b>TXT File Accepted!</b>\n"
        f"🔗 <b>Valid URLs Found:</b> {len(valid_urls)}"
        f"{dup_note}{truncated_note}\n\n"
        f"⏳ Waiting for an available worker in the queue...",
        parse_mode=ParseMode.HTML,
    )

    username = f"@{user.username}" if user.username else user.first_name
    await qm.enqueue_job(
        user_id=user.id,
        username=username,
        chat_id=chat.id,
        urls=valid_urls,
        status_message_id=status_msg.message_id,
    )


async def process_job(app: Application, job: Job) -> None:
    """
    Executed by a background QueueManager worker.
    Processes each URL in the job sequentially, updates status, uploads videos,
    cleans up per-video files, and posts a final summary.
    """
    loop = asyncio.get_running_loop()
    config.total_jobs_processed += 1

    # Reconstruct a Message object reference for editing status
    try:
        status_msg: Message = await app.bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=f"🚀 <b>Starting Job <code>{job.job_id}</code></b>\nTotal URLs to process: <b>{job.total_urls}</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        status_msg = await app.bot.send_message(
            chat_id=job.chat_id,
            text=f"🚀 <b>Starting Job <code>{job.job_id}</code></b>\nTotal URLs to process: <b>{job.total_urls}</b>",
            parse_mode=ParseMode.HTML,
        )

    for idx, url in enumerate(job.urls, start=1):
        if job.cancel_event.is_set():
            break

        job.current_index = idx
        try:
            await status_msg.edit_text(
                f"⬇️ <b>Preparing Download:</b> {idx}/{job.total_urls}\n"
                f"🔗 <code>{html.escape(url[:80])}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

        tracker = ProgressTracker(
            loop=loop,
            status_message=status_msg,
            current_index=idx,
            total_urls=job.total_urls,
            cancel_event=job.cancel_event,
        )

        video_result = None
        try:
            video_result = await download_video_async(
                url=url,
                job_dir=job.temp_dir,
                index=idx,
                tracker=tracker,
            )

            if job.cancel_event.is_set():
                raise DownloadCancelledError("Job cancelled right before upload.")

            # Update status to Upload phase (Honest upload indicator, no fake percentages)
            size_str = format_bytes(video_result.file_size_bytes)
            try:
                await status_msg.edit_text(
                    f"⬆️ <b>Uploading to Telegram:</b> {idx}/{job.total_urls}\n"
                    f"🎬 <b>Title:</b> {html.escape(video_result.title[:80])}\n"
                    f"📦 <b>Size:</b> {size_str}\n"
                    f"<i>Please wait while Telegram receives the video stream...</i>",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass

            await app.bot.send_chat_action(chat_id=job.chat_id, action=ChatAction.UPLOAD_VIDEO)

            caption_html = render_caption(
                title=video_result.title,
                duration_sec=video_result.duration,
                size_bytes=video_result.file_size_bytes,
            )

            with open(video_result.file_path, "rb") as video_file:
                thumb_file = (
                    open(video_result.thumbnail_path, "rb")
                    if video_result.thumbnail_path and video_result.thumbnail_path.exists()
                    else None
                )
                try:
                    await app.bot.send_video(
                        chat_id=job.chat_id,
                        video=video_file,
                        caption=caption_html,
                        parse_mode=ParseMode.HTML,
                        duration=video_result.duration or None,
                        width=video_result.width or None,
                        height=video_result.height or None,
                        thumbnail=thumb_file,
                        supports_streaming=True,
                        read_timeout=180,
                        write_timeout=180,
                        connect_timeout=60,
                    )
                finally:
                    if thumb_file:
                        thumb_file.close()

            job.completed_count += 1
            config.total_videos_uploaded += 1

        except DownloadCancelledError:
            logger.info("Job %s cancelled by user %s at URL %d/%d", job.job_id, job.user_id, idx, job.total_urls)
            break

        except DownloadError as de:
            job.failed_count += 1
            config.total_Download_failures += 1
            await app.bot.send_message(
                chat_id=job.chat_id,
                text=(
                    f"⚠️ <b>Skipped URL ({idx}/{job.total_urls}):</b>\n"
                    f"🔗 <code>{html.escape(url[:100])}</code>\n"
                    f"<b>Reason:</b> {html.escape(str(de))}"
                ),
                parse_mode=ParseMode.HTML,
            )

        except TelegramError as te:
            job.failed_count += 1
            config.total_Download_failures += 1
            logger.error("Telegram upload error on job %s: %s", job.job_id, te)
            await app.bot.send_message(
                chat_id=job.chat_id,
                text=(
                    f"❌ <b>Telegram Upload Failed ({idx}/{job.total_urls}):</b>\n"
                    f"🔗 <code>{html.escape(url[:100])}</code>\n"
                    f"<b>Reason:</b> {html.escape(str(te))}"
                ),
                parse_mode=ParseMode.HTML,
            )

        except Exception as exc:
            job.failed_count += 1
            config.total_Download_failures += 1
            logger.exception("Unexpected error processing URL %d in job %s", idx, job.job_id)
            await app.bot.send_message(
                chat_id=job.chat_id,
                text=(
                    f"❌ <b>Unexpected Error ({idx}/{job.total_urls}):</b>\n"
                    f"🔗 <code>{html.escape(url[:100])}</code>\n"
                    f"<b>Reason:</b> {html.escape(str(exc))}"
                ),
                parse_mode=ParseMode.HTML,
            )

        finally:
            # Always clean up the current video's files immediately to preserve Render disk space
            if video_result:
                video_result.file_path.unlink(missing_ok=True)
                if video_result.thumbnail_path:
                    video_result.thumbnail_path.unlink(missing_ok=True)
            for leftover in job.temp_dir.glob(f"*_{idx:03d}.*"):
                leftover.unlink(missing_ok=True)

    # Final Job Summary
    if job.cancel_event.is_set():
        summary_text = (
            f"🛑 <b>Job <code>{job.job_id}</code> Cancelled</b>\n\n"
            f"• <b>Total URLs:</b> {job.total_urls}\n"
            f"• <b>Completed:</b> {job.completed_count}\n"
            f"• <b>Failed:</b> {job.failed_count}\n"
            f"• <b>Remaining Skipped:</b> {max(0, job.total_urls - job.completed_count - job.failed_count)}\n"
            "🧹 Temporary files have been cleaned up."
        )
    else:
        summary_text = (
            f"✅ <b>Job <code>{job.job_id}</code> Finished!</b>\n\n"
            f"• <b>Total URLs:</b> {job.total_urls}\n"
            f"• <b>Successfully Uploaded:</b> {job.completed_count}\n"
            f"• <b>Failed:</b> {job.failed_count}\n"
            "🧹 All temporary files cleaned."
        )

    try:
        await status_msg.edit_text(summary_text, parse_mode=ParseMode.HTML)
    except Exception:
        await app.bot.send_message(chat_id=job.chat_id, text=summary_text, parse_mode=ParseMode.HTML)


async def handle_non_txt_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Friendly reminder when a user uploads a non-TXT document."""
    if update.effective_message:
        await update.effective_message.reply_text(
            "⚠️ Please upload a <code>.txt</code> file containing one video URL per line.",
            parse_mode=ParseMode.HTML,
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler so unhandled telegram exceptions never crash the polling loop."""
    logger.error("Global exception caught in handler:", exc_info=context.error)


# ============================================================================
# Application Lifecycle Hooks
# ============================================================================

async def post_init(application: Application) -> None:
    """Initialize the QueueManager and start worker tasks once the event loop is running."""
    qm = QueueManager(
        max_concurrent_jobs=config.max_concurrent_jobs,
        job_processor=lambda job: process_job(application, job),
    )
    application.bot_data["queue_manager"] = qm
    await qm.start_workers()
    logger.info("Bot initialized and polling started.")


async def post_stop(application: Application) -> None:
    """Gracefully stop background workers and remove temp files on SIGINT/SIGTERM."""
    qm: QueueManager = application.bot_data.get("queue_manager")
    if qm:
        await qm.stop_workers()

class _HealthCheckHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler so Render Web Services detect an open port."""
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - TXT Leech Bot is running.")

    def log_message(self, format: str, *args: object) -> None:
        # Suppress HTTP access logs to keep Render logs clean
        return


def start_dummy_http_server() -> None:
    """Bind to Render's PORT environment variable if present."""
    port_str = os.getenv("PORT")
    if not port_str:
        return
    port = int(port_str)
    server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health-check HTTP server listening on 0.0.0.0:%d", port)

def main() -> None:
    """Build and run the Telegram Bot Application."""
    start_dummy_http_server()

    application = (
        ApplicationBuilder()
        .token(config.bot_token)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    # ... keep the rest of main() exactly the same


    # User Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    # Admin Commands
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("jobs", jobs_command))
    application.add_handler(CommandHandler("setmaxurls", setmaxurls_command))
    application.add_handler(CommandHandler("setcaption", setcaption_command))
    application.add_handler(CommandHandler("allow", allow_user_command))
    application.add_handler(CommandHandler("deny", deny_user_command))
    application.add_handler(CommandHandler("private", private_mode_command))

    # Document Handlers
    application.add_handler(MessageHandler(filters.Document.TXT, handle_txt_document))
    application.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.TXT, handle_non_txt_document))

    # Global Error Handler
    application.add_error_handler(error_handler)

    # Start long polling (handles SIGINT and SIGTERM gracefully on Render)
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

