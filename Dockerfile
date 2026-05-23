# ===== Base Image: Python 3.10 slim for minimal size =====
FROM python:3.10-slim

# System-level dependencies for torch, transformers, and scraping
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first to exploit Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Expose FastAPI port
EXPOSE 8000

# Start the FastAPI server with uvicorn
# We use a shell-form CMD to allow environment variable expansion (like $PORT)
# Set workers back to 2 since RAM is no longer an issue with externalized models
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 75"]
