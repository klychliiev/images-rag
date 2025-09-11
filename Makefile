APP=app

.PHONY: build up down restart logs sh prune

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose down && docker compose up -d

logs:
	docker compose logs -f $(APP)

sh:
	docker compose exec $(APP) /bin/bash

prune:
	docker system prune -f
