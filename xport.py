#!/usr/bin/python3
'''
SAS .xpt (transport) versions 8 and 9 converter
'''
import sys, csv, logging  # pylint: disable=multiple-imports

def xpt_to_csv(filename=None, outfilename=None):
    '''
    convert xpt file to csv format
    '''
    infile = open(filename, 'rb') if filename is not None else sys.stdin
    outfile = open(outfilename, 'w') if outfilename is not None else sys.stdout
    csvout = csv.writer(outfile)
    while True:
        record = infile.read(80)
        if not record:
            logging.debug('conversion complete')
            break
        logging.debug('record: %r', record)
        csvout.writerow([record.rstrip(b'\0')])

if __name__ == '__main__':
    xpt_to_csv(*sys.argv[1:])
