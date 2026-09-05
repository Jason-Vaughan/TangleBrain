.PHONY: help venv lint test test-live clean

VENV := .venv
PY := $(VENV)/bin/python

help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS=":.*?## "} {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create the dev venv and install editable with the delegate + dev extras.
	@if [ ! -d $(VENV) ]; then python3 -m venv $(VENV); fi
	@$(PY) -m pip install --quiet --upgrade pip
	@$(PY) -m pip install --quiet -e ".[delegate,dev]"
	@echo "venv: ready ($(VENV))"

lint: venv ## Lint (ruff) and type-check (mypy). Both gate CI via the `test` target.
	@$(VENV)/bin/ruff check tanglebrain tests
	@$(VENV)/bin/mypy
	@echo "lint: OK"

test: venv lint ## Lint + type-check + run the unit test suite (hermetic; HTTP is mocked).
	@$(PY) -m unittest discover -s tests -p "test_*.py" -v

test-live: venv ## Opt-in: hit the real local endpoint your roster points at, end-to-end.
	@TANGLEBRAIN_LIVE=1 $(PY) -m unittest tests.test_live -v

clean: ## Remove the venv and build artifacts.
	@rm -rf $(VENV) build dist *.egg-info tanglebrain/*.egg-info
	@find . -name "__pycache__" -type d -prune -exec rm -rf {} +
	@echo "clean: OK"
