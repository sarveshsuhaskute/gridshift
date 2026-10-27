PY := .venv/bin/python
CDK := npx -y aws-cdk@2

.PHONY: install test lint synth bootstrap deploy destroy plan prices benchmark-submit benchmark-report

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check . && .venv/bin/ruff format --check .

synth:
	$(CDK) synth --quiet

# One-time per account and region: CDK needs a bootstrap stack in every region it deploys to.
bootstrap:
	ACCOUNT=$$(aws sts get-caller-identity --query Account --output text); \
	for region in $$($(PY) -c "import json; print(' '.join(json.load(open('cdk.json'))['context']['worker_regions']))"); do \
		$(CDK) bootstrap aws://$$ACCOUNT/$$region; \
	done

deploy:
	$(CDK) deploy --all --require-approval broadening

destroy:
	$(CDK) destroy --all --force

plan:
	$(PY) scripts/plan_locally.py

prices:
	$(PY) scripts/refresh_prices.py

benchmark-submit:
	$(PY) scripts/benchmark.py submit --count 20

benchmark-report:
	$(PY) scripts/benchmark.py report
