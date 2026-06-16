.PHONY: help venv lint test test-live clean

VENV := .venv
PY := $(VENV)/bin/python

help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS=":.*?## "} {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create the dev venv and install editable with the delegate extra (httpx/PyYAML/mcp).
	@if [ ! -d $(VENV) ]; then python3 -m venv $(VENV); fi
	@$(PY) -m pip install --quiet --upgrade pip
	@$(PY) -m pip install --quiet -e ".[delegate]"
	@echo "venv: ready ($(VENV))"

lint: ## Smoke-check that all Python files parse cleanly.
	@find tanglebrain tests -name "*.py" -not -path "*/__pycache__/*" -exec python3 -m py_compile {} +
	@echo "lint: OK"

test: lint venv ## Lint + run the unit test suite (hermetic; HTTP is mocked).
	@$(PY) -m unittest discover -s tests -p "test_*.py" -v

test-live: venv ## Opt-in: hit the real local LiteLLM endpoint end-to-end (needs the scoped key).
	@TANGLEBRAIN_LIVE=1 $(PY) -m unittest tests.test_live -v

clean: ## Remove the venv and build artifacts.
	@rm -rf $(VENV) build dist *.egg-info tanglebrain/*.egg-info
	@find . -name "__pycache__" -type d -prune -exec rm -rf {} +
	@echo "clean: OK"
