FROM node:20-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        xvfb \
        xauth \
        x11-utils \
        python3 \
        python3-flask \
        curl \
        ca-certificates \
        dumb-init \
        procps \
        psmisc \
        util-linux \
        fonts-liberation \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/chromium /usr/bin/google-chrome \
    && ln -sf /usr/bin/chromium /usr/bin/google-chrome-stable

ARG WEREAD_CLI_VERSION=0.18.0
RUN npm install -g weread-selenium-cli@${WEREAD_CLI_VERSION} \
    && npm cache clean --force

RUN userdel -r node 2>/dev/null || true \
    && useradd -m -u 1000 -s /bin/bash user \
    && mkdir -p /data/.weread \
    && chown -R user:user /data

WORKDIR /app
COPY --chown=user:user app.py entrypoint.sh start_reading.sh ./
RUN chmod +x entrypoint.sh start_reading.sh \
    && chown -R user:user /app

ENV HOME=/home/user \
    DISPLAY=:99 \
    PORT=7860 \
    PYTHONUNBUFFERED=1 \
    NODE_ENV=production \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_BIN=/usr/bin/chromedriver \
    WEREAD_BROWSER=chrome \
    WEREAD_DATA_DIR=/data/.weread \
    WEREAD_DURATION=68 \
    WEREAD_SPEED=slow \
    WEREAD_SELECTION=2 \
    WEREAD_SCREENSHOT=true \
    WEREAD_AGREE_TERMS=true \
    READING_INTERVAL_HOURS=12 \
    SELF_PING_MINUTES=5

USER user
EXPOSE 7860

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:7860/status >/dev/null || exit 1

ENTRYPOINT ["dumb-init", "--", "/app/entrypoint.sh"]
