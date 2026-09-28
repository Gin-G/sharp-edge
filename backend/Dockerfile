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

# Xvfb gives the headed Chromium somewhere to draw. The API itself never
# touches the display — the browser is only launched when someone logs in to
# DraftKings — so Xvfb just needs to be up before that happens.
#
# This deliberately does NOT use xvfb-run. xvfb-run starts the X server in a
# subshell with SIGUSR1 ignored, so the server signals readiness back to its
# parent, then sigsuspends waiting for that signal. As PID 1 the handshake
# never completes and xvfb-run hangs forever: Xvfb comes up, the command is
# never launched, nothing is logged, and nothing ever binds port 8000. The
# container looks perfectly healthy from outside — which, with no probes on
# the Deployment, is exactly how it went unnoticed for two days.
#
# exec keeps the API as PID 1 so it receives SIGTERM directly and uvicorn can
# shut down gracefully on rollout, instead of being SIGKILLed when the
# termination grace period runs out.
CMD ["sh", "-c", "Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp & export DISPLAY=:99; exec sharp-edge-api"]
