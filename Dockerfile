# A small, host-agnostic image. Works on Railway, Fly.io, Render, a VPS,
# or anywhere that runs containers.
FROM python:3.12-slim

# Don't write .pyc files; flush logs immediately so they show up in your host's log viewer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so this layer is cached between code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the bot.
COPY . .

# Run as a non-root user.
RUN useradd --create-home appuser
USER appuser

CMD ["python", "bot.py"]
