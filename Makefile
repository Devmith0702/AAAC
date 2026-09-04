# Makefile for AAAC project

.PHONY: run stop test lint logs

MODE ?= aaac

run:
	AAAC_MODE=$(MODE) docker compose up -d

stop:
	docker compose down

logs:
	docker compose logs -f

test:
	pytest

lint:
	ruff check src && mypy src
