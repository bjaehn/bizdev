FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BD_CONFIG=/app/config.yaml BD_DATA_DIR=/data PLAYWRIGHT_BROWSERS_PATH=/opt/browsers

WORKDIR /app
COPY pyproject.toml ./
COPY bd ./bd
RUN pip install '.[ui,pdf]'
# PDF export needs a headless Chromium (about 300 MB with its system libraries). If this step fails the build still succeeds: the Export PDF links stay hidden and
# `bd check` says "PDF export: not installed", so a browser problem can never stop a deploy.
RUN (playwright install --with-deps --only-shell chromium && chmod -R a+rX /opt/browsers) || echo "PDF export: the Chromium install failed; the app runs without it"; \
    rm -rf /var/lib/apt/lists/*
COPY config.yaml accounts.csv exclusions.csv pe_firms.csv pe_companies.csv pe_relationships.csv ./
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && useradd --system --uid 10001 --home-dir /app bd

EXPOSE 8080
# Starts as root only to fix ownership of the (root-owned) mounted volume, then drops to the unprivileged user.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bd", "serve"]
