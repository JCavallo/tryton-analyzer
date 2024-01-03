test:
	pytest --cov-report term-missing --cov-report=html --cov-branch \
		   --cov tryton_analyzer/

lint:
	@echo
	flake8 .
	@echo
	mypy .
	@echo
	isort --diff -c .
	@echo
	black --check --diff --color .
	@echo
	bandit -r tryton-analyzer/
	# @echo
	# pip-audit

format:
	isort .
	black .
	pyupgrade --py311-plus **/*.py

install_hooks:
	scripts/install_hooks.sh
