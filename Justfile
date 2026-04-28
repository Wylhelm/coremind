# CoreMind — developer workflow
# Install just: https://github.com/casey/just  or  `cargo install just`

# Show available recipes by default
default:
    @just --list

# --- Environment ---

# Create venv and install dev dependencies
setup:
    python3.12 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -e ".[dev]"

# Remove the venv (not touching caches)
clean-venv:
    rm -rf .venv

# Remove caches
clean:
    rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

# --- Code quality ---

# Run all linters
lint: lint-ruff lint-mypy

lint-ruff:
    .venv/bin/ruff check .
    .venv/bin/ruff format --check .

lint-mypy:
    .venv/bin/mypy src/ plugins/systemstats/ tests/plugins/systemstats/ plugins/homeassistant/ tests/plugins/homeassistant/ integrations/openclaw-adapter/coremind_side/ tests/integrations/openclaw_adapter/

# Auto-fix what can be fixed
fix:
    .venv/bin/ruff check --fix .
    .venv/bin/ruff format .

# --- Specs ---

# Validate that worldevent.schema.json is a valid JSON Schema (Draft 2020-12)
# and that all example payloads in the prose spec validate against it.
spec-validate:
    .venv/bin/python scripts/validate_specs.py

# --- Protobuf ---

# Regenerate Python stubs from .proto files
proto-gen:
    mkdir -p src/coremind/plugin_api/_generated
    .venv/bin/python -m grpc_tools.protoc \
        --proto_path=spec \
        --python_out=src/coremind/plugin_api/_generated \
        --grpc_python_out=src/coremind/plugin_api/_generated \
        --pyi_out=src/coremind/plugin_api/_generated \
        spec/plugin.proto
    touch src/coremind/plugin_api/_generated/__init__.py
    mkdir -p integrations/openclaw-adapter/coremind_side/coremind_plugin_openclaw/_generated
    .venv/bin/python -m grpc_tools.protoc \
        --proto_path=spec \
        --proto_path=integrations/openclaw-adapter/proto \
        --python_out=integrations/openclaw-adapter/coremind_side/coremind_plugin_openclaw/_generated \
        --grpc_python_out=integrations/openclaw-adapter/coremind_side/coremind_plugin_openclaw/_generated \
        --pyi_out=integrations/openclaw-adapter/coremind_side/coremind_plugin_openclaw/_generated \
        integrations/openclaw-adapter/proto/adapter.proto
    touch integrations/openclaw-adapter/coremind_side/coremind_plugin_openclaw/_generated/__init__.py
    # Rewrite generated imports to avoid flat-module collisions.
    sed -i 's|^import plugin_pb2 as plugin__pb2|from coremind.plugin_api._generated import plugin_pb2 as plugin__pb2|' \
        integrations/openclaw-adapter/coremind_side/coremind_plugin_openclaw/_generated/adapter_pb2.py \
        integrations/openclaw-adapter/coremind_side/coremind_plugin_openclaw/_generated/adapter_pb2_grpc.py
    sed -i 's|^import adapter_pb2 as adapter__pb2|from coremind_plugin_openclaw._generated import adapter_pb2 as adapter__pb2|' \
        integrations/openclaw-adapter/coremind_side/coremind_plugin_openclaw/_generated/adapter_pb2_grpc.py

# CI check: ensure generated files are committed (no git diff after regen)
proto-gen-check: proto-gen
    @if ! git diff --quiet -- src/coremind/plugin_api/_generated; then \
        echo "❌ Generated proto stubs are out of date. Run 'just proto-gen' and commit."; \
        git diff --stat -- src/coremind/plugin_api/_generated; \
        exit 1; \
    fi
    @echo "✅ Generated proto stubs are up to date."

# --- Tests ---

# Run unit tests (fast, no external services)
test:
    .venv/bin/pytest -m "not integration and not e2e"

# Run integration tests (requires docker-compose)
test-integration:
    .venv/bin/pytest -m integration

# Run release-gating end-to-end scenarios (no external services)
test-scenarios:
    .venv/bin/pytest -m e2e

# Run all tests
test-all:
    .venv/bin/pytest

# --- All-in-one ---

# Everything CI runs
ci: lint spec-validate proto-gen-check test test-scenarios

# Quick pre-commit check
pre-commit: fix lint
