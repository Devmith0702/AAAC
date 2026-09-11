# Mock origin (CLAUDE.md §4.1). Serves http://origin:8002.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# configs/ and results/ are bind-mounted by docker-compose.yml so a run can be
# re-parameterised without rebuilding the image.
EXPOSE 8002

CMD ["python", "-m", "aaac.origin"]
