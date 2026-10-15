FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BD_CONFIG=/app/config.yaml BD_DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml ./
COPY bd ./bd
RUN pip install '.[ui]'
COPY config.yaml accounts.csv exclusions.csv ./
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && useradd --system --uid 10001 --home-dir /app bd

EXPOSE 8080
# Starts as root only to fix ownership of the (root-owned) mounted volume, then drops to the unprivileged user.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bd", "serve"]
