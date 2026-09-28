# Test image for the c4_relay integration: real Home Assistant core via
# pytest-homeassistant-custom-component (needs Linux wheels, hence Docker).
#   docker build -t c4relay-hatest ha/tests
#   docker run --rm -v "%cd%:/work" -w /work c4relay-hatest pytest ha/tests -q
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest-homeassistant-custom-component
