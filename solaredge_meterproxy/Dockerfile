ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.21
FROM ${BUILD_FROM}

# aiohttp from Alpine (no compiler needed), pure-Python deps pinned via pip
RUN apk add --no-cache python3 py3-pip py3-aiohttp

COPY app/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY app /app
WORKDIR /app

# with-contenv: container env (SUPERVISOR_TOKEN) under the s6 init of the base image
# -u: unbuffered, so every log line shows up in the add-on log immediately
CMD ["/usr/bin/with-contenv", "python3", "-u", "-m", "semp"]
