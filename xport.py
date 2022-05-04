#!/usr/bin/python3
'''
SAS .xpt (transport) versions 8 and 9 converter
'''
import sys

def xpt_to_csv(filename=None, outfilename=None):
    infile = open(filename, 'rb') if filename is not None else sys.stdin
    outfile = open(outfilename, 'w') if outfilename is not None else sys.stdout
    infile.close()
    outfile.close()

if __name__ == '__main__':
    xpt_to_csv(*sys.argv[1:])
