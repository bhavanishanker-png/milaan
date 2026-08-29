.PHONY: setup generate run evaluate ablation sweep test lint demo clean verify

PY := python3
SEED_A := 20260101
SEED_B := 20260202
SEED_C := 20260303

setup:
	pip install -e ".[dev]"

generate:
	$(PY) -m milaan.generate.cli --batch a --seed $(SEED_A) --records 1000 --out data/a
	$(PY) -m milaan.generate.cli --batch b --seed $(SEED_B) --records 1000 --out data/b
	$(PY) -m milaan.generate.cli --batch c --seed $(SEED_C) --records 1000 --out data/c
	@echo "Batch C is HELD OUT. Do not read it until Phase 8."

run:
	$(PY) -m milaan.pipeline --data data/a --out out/a

evaluate:
	$(PY) eval/evaluate.py --run out/a --labels data/a/labels.json

ablation:
	$(PY) eval/ablation.py --data data/b --out out/ablation

sweep:
	$(PY) eval/sweep.py --data data/b --out out/sweep

# Phase 8 only. Runs once, after code freeze. Numbers go straight to the README.
verify:
	@echo "Held-out run on batch C. This should happen exactly once."
	$(PY) -m milaan.pipeline --data data/c --out out/c
	$(PY) eval/evaluate.py --run out/c --labels data/c/labels.json

test:
	pytest

lint:
	ruff check src eval tests

demo:
	streamlit run ui/app.py

clean:
	rm -rf out .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -exec rm -rf {} +
