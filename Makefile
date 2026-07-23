.PHONY: up down build logs worker api shell migrate clean

up:            ## Build + start the full stack
	docker compose up -d --build

down:          ## Stop everything
	docker compose down

build:         ## Rebuild images
	docker compose build

logs:          ## Tail worker logs (where the pipeline runs)
	docker compose logs -f worker

api-logs:      ## Tail API logs
	docker compose logs -f api

shell:         ## Shell into the API container
	docker compose exec api bash

migrate:       ## Apply DB migrations
	docker compose exec api alembic upgrade head

clean:         ## Remove containers + volumes (DESTROYS data)
	docker compose down -v
