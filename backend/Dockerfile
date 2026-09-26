FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium for the DraftKings adapter, plus Xvfb to give it a display.
#
# Its auth endpoints sit behind Akamai Bot Manager, whose _abck cookie only
# validates once the sensor JS has posted telemetry — so the login runs in a
# real browser rather than an HTTP client. Measured against the live login
# page: headless-shell 403, new headless mode 403, headed 200. Akamai rejects
# headless Chromium either way, so the browser has to run headed, and headed
# needs a display. Hence Xvfb.
#
# `--with-deps` pulls the system libraries Chromium needs, which the slim base
# does not carry. This layer is the bulk of the image (~400MB); for a
# FanDuel-only build, drop it and remove playwright from requirements.txt —
# the DraftKings routes then answer 501 naming the missing browser and nothing
# else changes.
RUN playwright install --with-deps chromium && \
    apt-get update && apt-get install -y --no-install-recommends xvfb && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir --no-deps -e .

EXPOSE 8000

# xvfb-run gives the headed Chromium somewhere to draw. -a picks a free
# display number, so nothing breaks if one is already taken. The API itself
# is unaffected — it never touches the display, and the browser is only
# launched when someone logs in to DraftKings.
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1280x1024x24", "sharp-edge-api"]
