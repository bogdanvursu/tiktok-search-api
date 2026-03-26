FROM python:3.11-slim

# Dependențe sistem pentru Playwright Chromium
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxcb1 libxkbcommon0 libx11-6 \
    libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
    fonts-liberation libappindicator3-1 xdg-utils \
    iproute2 procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiere și instalare dependențe Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalare Playwright Chromium
RUN playwright install chromium
RUN playwright install-deps chromium

# Copiere cod sursă
COPY . .

EXPOSE 5001

CMD ["python3", "app.py"]
