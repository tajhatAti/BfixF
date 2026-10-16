# BfixF — bot + website in one container.
#
#   docker build -t bfixf .
#   docker run -d --name bfixf --env-file config.env -p 8080:8080 -v bfixf-data:/data bfixf
#
# Everything the process writes (SQLite database, Telegram sessions, logs) lives
# in /data, so the volume is the only thing you need to back up.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_FILE=/data/bot_data.sqlite3 \
    LOG_FILE=/data/logs/bot.log \
    SESSION_STATE_FILE=/data/bot_master.session \
    WEB_HOST=0.0.0.0 \
    PORT=8080

WORKDIR /app

# Only the dependency list first: an edit to the code does not re-install pip packages.
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data/logs

VOLUME ["/data"]
EXPOSE 8080

# The website is a plain HTTP health check; the bot retries Telegram on its own.
HEALTHCHECK --interval=60s --timeout=8s --start-period=25s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/health').read()"

CMD ["python", "main.py"]
