# Admission service, M1 (CLAUDE.md §3.5). Serves http://admission:8000.
#
# Written by M3 as testbed wiring: it installs and runs M1's package unmodified.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY README.md ./
# See the note in origin.Dockerfile: retry rather than fail a long download.
RUN pip install --no-cache-dir --retries 5 --timeout 60 .

# configs/ and results/ are bind-mounted by docker-compose.yml so a run can be
# re-parameterised without rebuilding the image.
EXPOSE 8000

CMD ["uvicorn", "aaac.admission.api:app", "--host", "0.0.0.0", "--port", "8000"]
