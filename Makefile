.PHONY: demo test golden linecount clean

demo:
	python -m lakecommit demo --workdir .lakecommit-demo

test:
	pytest -q

golden:
	python -m lakecommit verify-golden --workdir .lakecommit-demo

linecount:
	python -m lakecommit linecount

clean:
	rm -rf .lakecommit-demo .pytest_cache
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
