.PHONY: up down logs logs-guard test smoke aws-up aws-down aws-logs aws-build

# --- Local demo (ministack + seed + generator + guard) ---

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

# --- Real AWS (guard only; requires .env.aws + config.aws.yaml) ---

aws-build:
	docker build -t bedrock-budget-guard .

aws-up:
	docker compose -f docker-compose.aws.yaml up -d --build

aws-down:
	docker compose -f docker-compose.aws.yaml down -v

aws-logs:
	docker compose -f docker-compose.aws.yaml logs -f budget-guard
