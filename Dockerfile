FROM python:3.11-slim

# Instalar ffmpeg y dependencias del sistema
RUN apt-get update && \
    apt-get install -y ffmpeg curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar dependencias e instalar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# FUERZA la actualización de yt-dlp a la última versión de 2026
RUN pip install --no-cache-dir -U yt-dlp

# Verificar versiones para el log de Railway
RUN node --version && yt-dlp --version

COPY . .

CMD ["python", "bot.py"]