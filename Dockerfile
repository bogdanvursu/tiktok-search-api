# Use Microsoft's Playwright Docker image which has all dependencies pre-installed
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Dynamic PORT (Render.com sets $PORT automatically, default 10000)
ENV PORT=10000
ENV WORKER_PORT=8764
ENV PYTHONUNBUFFERED=1

EXPOSE $PORT

CMD ["python3", "app.py"]
