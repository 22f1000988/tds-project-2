FROM python:3.10-slim

# --- System deps required by Playwright browsers AND Tesseract ---
RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates curl unzip \
    # Playwright dependencies
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxkbcommon0 \
    libgtk-3-0 libgbm1 libasound2 libxcomposite1 libxdamage1 libxrandr2 \
    libxfixes3 libpango-1.0-0 libcairo2 \
    # Tesseract OCR engine
    tesseract-ocr \
    # FFmpeg for audio processing (pydub)
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# --- Install Playwright + Chromium ---
RUN pip install --no-cache-dir playwright && playwright install --with-deps chromium

# --- Install Python deps ---
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# --- Copy app to container ---
WORKDIR /app
COPY . .

ENV PYTHONUNBUFFERED=1
ENV PYTHONIOENCODING=utf-8
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# HuggingFace Spaces exposes port 7860
EXPOSE 7860

# --- Run your FastAPI app ---
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
