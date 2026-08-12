FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

# Chrome runtime dependencies + Xvfb (virtual display for headed Chrome).
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg xvfb libnss3 libx11-xcb1 libxcb1 \
    libxcomposite1 libxcursor1 libxdamage1 libxi6 libxtst6 libxrandr2 \
    libasound2 libatk-bridge2.0-0 libgtk-3-0 fonts-liberation \
    && curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
       | gpg --dearmor -o /usr/share/keyrings/google.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/google.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user (Chrome sandbox / good practice).
RUN useradd -m runner
USER runner

# Start a virtual display, then the app. Headed Chrome renders on the
# virtual screen (Turnstile passes) but nobody sees a window.
CMD ["sh", "-c", "Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp & exec python app.py"]
