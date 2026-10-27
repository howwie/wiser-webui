FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Run as an unprivileged user, who owns the folder where settings (the room order) are saved.
# docker-compose.yml mounts a named volume there, which copies this ownership on first use.
RUN useradd --system --no-create-home wiser && mkdir /srv/data && chown wiser /srv/data
ENV WISER_DATA_DIR=/srv/data
USER wiser

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

# The app ends its live-update streams on shutdown; the timeout is a backstop so a stuck
# connection can't hold up a restart (docker stop would otherwise force-kill after 10s).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-graceful-shutdown", "5"]
