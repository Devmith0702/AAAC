# Shaped client container (CLAUDE.md §4.2) and the iperf3 reflector used by
# `make verify-testbed`.
#
# iproute2      -> tc / ip, for the netem + tbf chain
# iputils-ping  -> RTT and loss measurement
# iperf3        -> achieved-throughput measurement in both directions
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        iproute2 \
        iputils-ping \
        iperf3 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY src/aaac/evaluation/testbed/netem.sh /usr/local/bin/netem.sh
RUN chmod +x /usr/local/bin/netem.sh

CMD ["sleep", "infinity"]
