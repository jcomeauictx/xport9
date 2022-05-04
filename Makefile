SOURCES := $(wildcard *.py)
LINT := $(SOURCES:.py=.lint)
TESTS := $(SOURCES:.py=.test)
PYTHON ?= python3
PYLINT ?= pylint3
export
all: test.csv
%.csv: xport.py %.xpt $(LINT) $(TESTS)
	$(PYTHON) $< $(word 2, $+)
%.lint: %.py
	$(PYLINT) $<
%.test: %.py
	$(PYTHON) -m doctest $<
