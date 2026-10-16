FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/app/data/launch-alert.sqlite3

WORKDIR /app

RUN groupadd --gid 10001 launch-alert \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin launch-alert \
    && mkdir /app/data /config \
    && chown 10001:10001 /app/data /config

COPY --chown=10001:10001 launch_alert/ /app/launch_alert/

# Unraid runs as nobody:users (99:100); allow any unprivileged UID to read code.
RUN chmod -R a+rX /app/launch_alert

USER 10001:10001

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-m", "launch_alert", "healthcheck"]

CMD ["python", "-m", "launch_alert", "run"]
