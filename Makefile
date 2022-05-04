SOURCES := $(wildcard *.py)
LINT := $(SOURCES:.py=.lint)
TESTS := $(SOURCES:.py=.test)
PYLINT ?= pylint3
export
all: test.csv
%.csv: xport.py %.xpt $(LINT) $(TESTS)
	./$< $(words 1, $+)
