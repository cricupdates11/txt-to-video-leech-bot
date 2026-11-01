# Advanced Telegram TXT-to-Video Leech Bot

A modular, asynchronous Telegram bot built with **Python 3.12**, **python-telegram-bot v22.x**, **yt-dlp**, and **FFmpeg**. It accepts `.txt` files containing multiple video URLs, downloads supported and authorized videos sequentially with live throttled progress updates, generates compliant thumbnails, and uploads streamable MP4 videos to Telegram.

## Features
- **TXT Batch Processing**: Ignores comments (`#`) and blank lines, deduplicates links, and validates URLs against SSRF (blocks localhost/private IPs).
- **Live Download Progress**: Throttled real-time percentage, speed, and byte counters.
- **Thumbnail & Metadata Extraction**: Automatic `ffprobe` inspection and `ffmpeg` thumbnail generation.
- **Multi-User Queue & Isolation**: Configurable worker pool (`MAX_CONCURRENT_JOBS`) with isolated per-job temporary folders and immediate file cleanup.
- **Instant Cancellation**: `/cancel` aborts active `yt-dlp` downloads safely via progress hooks.
- **Admin Controls**: Runtime control over `MAX_URLS`, custom captions, user allowlists, and Private Mode.

## Commands
### User Commands
- `/start` - Start the bot
- `/help` - Show usage instructions and active limits
- `/status` - View current job progress
- `/cancel` - Cancel your running or queued job
- `/id` - View your Telegram User ID
- `/about` - Show bot architecture details

### Admin Commands
- `/stats` - View bot statistics and configuration
- `/jobs` - View all active and queued user jobs
- `/setmaxurls <number>` - Change maximum URLs allowed per TXT file
- `/setcaption <template>` - Customize caption template (`{title}`, `{duration}`, `{size}`)
- `/allow <user_id>` - Authorize a user in Private Mode
- `/deny <user_id>` - Revoke user authorization
- `/private <on|off>` - Toggle Private Mode

## Deployment on Render (Background Worker)
1. Push this repository to GitHub (never commit your `.env` file).
2. In the [Render Dashboard](https://dashboard.render.com/), click **New +** -> **Blueprint**.
3. Connect your GitHub repository containing `render.yaml`.
4. Enter your `BOT_TOKEN` and `ADMIN_IDS` when prompted.
5. Click **Apply** to build the Docker container and launch the Background Worker.
