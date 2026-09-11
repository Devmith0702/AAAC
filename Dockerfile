FROM python:3.11-slim

WORKDIR /app

# For now, just a placeholder command to keep the container running
CMD ["tail", "-f", "/dev/null"]
