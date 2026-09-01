.DEFAULT_GOAL := dev

.PHONY: dev
dev:
	uv run -m uvicorn getgather.main:app --reload --host 127.0.0.1 --port 23456

.PHONY: format-frontend
format-frontend:
	npx --yes prettier --write "**/*.{html,js,jsx,ts,tsx,css,json,md}" "!**/rfb.min.js"

.PHONY: check-frontend-format
check-frontend-format:
	npx --yes prettier --check "**/*.{html,js,jsx,ts,tsx,css,json,md}" "!**/rfb.min.js"

.PHONY: format-backend
format-backend:
	uv run ruff format
	uv run ruff check --fix

.PHONY: check-backend-format
check-backend-format:
	uv run ruff check
	uv run ruff format --check

.PHONY: format-yaml
format-yaml:
	uv run yamlfix $$(find . -type f \( -name '*.yml' -o -name '*.yaml' \) | grep -v -E '\.venv/|node_modules/|mcp-tools\.yaml')

.PHONY: check-yaml-format
check-yaml-format:
	uv run yamlfix --check $$(find . -type f \( -name '*.yml' -o -name '*.yaml' \) | grep -v -E '\.venv/|node_modules/|mcp-tools\.yaml')

.PHONY: format
format: format-backend format-yaml format-frontend

.PHONY: typecheck
typecheck:
	uv run ty check
	uv run pyright .

.PHONY: test
test:
	uv run pytest --no-header --tb=no -q -m "not api and not mcp and not distill"

.PHONY: check-patterns
check-patterns:
	uv run python scripts/check_pattern_testids.py

.PHONY: check
check: check-backend-format check-yaml-format check-frontend-format typecheck check-patterns

.PHONY: package
package:
	uv run --group dev pyinstaller remotebrowser-mcp.spec
