SOURCES := $(wildcard *.py)
LINT := $(SOURCES:.py=.lint)
TESTS := $(SOURCES:.py=.test)
PYTHON ?= python3
PYLINT ?= pylint3
OUTFILE ?=
export
all: test.csv
%.csv: xport.py %.xpt $(LINT) $(TESTS) .FORCE
	$(PYTHON) $(OPT) $< $(word 2, $+) $@
%.lint: %.py
	$(PYLINT) $<
%.test: %.py
	$(PYTHON) -m doctest $<
longtest: /tmp/long.xpt
	$(MAKE) OPT=-OO $(<:.xpt=.csv)
.FORCE:
