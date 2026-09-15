# Estimator/delivery service, M2 (CLAUDE.md §3.5). Serves http://delivery:8001.
#
# Written by M3 as testbed wiring: it installs and runs M2's package unmodified.
# The payload templates and their sub-resources travel in the installed package
# (see [tool.setuptools.package-data] in pyproject.toml) — the variants are the
# thing under measurement, so they must not depend on a bind mount.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY README.md ./
RUN pip install --no-cache-dir .

# models/ carries the exported classifier the estimator loads at request time.
COPY models ./models

EXPOSE 8001

CMD ["uvicorn", "aaac.delivery.app:app", "--host", "0.0.0.0", "--port", "8001"]
