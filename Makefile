PYTHON3=python3

server:
	$(PYTHON3) -m bustag.app.index

javbus:
	$(PYTHON3) -m bustag.main download

recommend:
	$(PYTHON3) -m bustag.main recommend

check:
	$(PYTHON3) -m bustag.main check

test:
	$(PYTHON3) -m pytest

build:
	docker build -t bustag:dev .

run:
	docker run --rm -d --name bustag -v "$(CURDIR)/data:/app/data" -p 8000:8000 bustag:dev

.PHONY: server javbus recommend check test build run
