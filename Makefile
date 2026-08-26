.PHONY: up down logs logs-guard test smoke

up:
	docker compose up -d --build

down:
	docker compose down -v

logs:
	docker compose logs -f generator

logs-guard:
	docker compose logs -f budget-guard

test:
	docker compose build generator
	docker compose run --rm --no-deps generator pytest -q
	docker compose build budget-guard
	docker compose run --rm --no-deps budget-guard pytest -q

smoke:
	docker compose --profile smoke run --rm --build smoke
