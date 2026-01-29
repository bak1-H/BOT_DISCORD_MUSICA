FROM python:3.11-slim

# Dependencias del sistema (ffmpeg + SSL + node 20)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      curl \
      ca-certificates \
      gnupg && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -U yt-dlp

# Log de versiones (útil en Railway)
RUN node --version && python --version && yt-dlp --version && ffmpeg -version

COPY . .

CMD ["python", "bot.py"]