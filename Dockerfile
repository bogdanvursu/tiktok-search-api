# Playwright + Python — all Chromium dependencies pre-installed
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default port — override with -e PORT=xxxx or in .env
ENV PORT=5001
ENV PYTHONUNBUFFERED=1
# Optional proxy — set to e.g. http://user:pass@host:port or socks5://host:port
ENV PROXY_SERVER=""

EXPOSE $PORT

CMD ["python3", "app.py"]
