# transcriber — the whole suite runs offline, with no credential and no network.
#
# There is nothing to install. Every import in src/ and tests/ is from the standard
# library, deliberately: a service that must run unattended for years should have nothing
# underneath it that can rot, and a test suite that needed a runner would quietly make that
# one dependency instead of none.

PYTHON ?= python3
SRC    := src
PKG    := $(SRC)/transcriber

export PYTHONPATH := $(SRC)

.PHONY: help test check selftest lint clean

help:
	@echo "make test      run the test suite (offline, no credentials)"
	@echo "make check     compile every module, then run the suite"
	@echo "make selftest  the service proving itself the way it does in production"
	@echo "make clean     remove __pycache__ and stray test scratch"

# The suite. -b keeps a module's own stdout out of the report; failures still print in full.
test:
	$(PYTHON) -m unittest discover -s tests -t . -v -b

# What CI should run: the code has to import and compile before its behaviour means
# anything, and a syntax error in a module no test touches is still a broken deploy.
check:
	$(PYTHON) -m compileall -q $(PKG) tests
	$(PYTHON) -m unittest discover -s tests -t . -b

# The same discipline as graph_pull.py --selftest downstream: the service proves parsing,
# the state machine, quote verification and the markdown contract with no credential.
selftest:
	$(PYTHON) -m transcriber selftest

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
