FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable unbuffered logging for Render
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install FFmpeg (includes ffprobe) and CA certificates, then clean apt cache
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy project source files
COPY . .

# Run as a non-root user for container security
RUN useradd -m botuser && chown -R botuser:botuser /app
USER botuser

# Start the Telegram bot worker
CMD ["python", "bot.py"]

