FROM python:3.10-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install all system dependencies for Playwright Chromium + OCR + audio
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates curl unzip apt-utils \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 \
    libgtk-3-0 libgbm1 libasound2 libxcomposite1 libxdamage1 libxrandr2 \
    libxfixes3 libpango-1.0-0 libcairo2 libglib2.0-0 \
    tesseract-ocr ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright Python package
RUN pip install --no-cache-dir playwright

# Install Chromium browsers + dependencies
RUN playwright install --with-deps chromium

# Workdir
WORKDIR /app

# Install Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy application code
COPY . .

# Environment vars
ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8
ENV PLAYWRIGHT_BROWSERS_PATH=/root/.cache/ms-playwright

# Expose HF Spaces port
EXPOSE 7860

# Start FastAPI server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
