FROM python:3.12-slim

WORKDIR /app

# System utilities + Tesseract OCR (for scanned PDF fallback)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium AND all its Linux system dependencies in one step
RUN playwright install --with-deps chromium

# Application code
COPY . .

# Ensure runtime output directories exist
RUN mkdir -p output/reviews output/pdf

EXPOSE 8000

CMD ["python", "run.py"]
