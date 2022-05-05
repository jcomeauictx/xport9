#!/usr/bin/python3
'''
SAS .xpt (transport) versions 8 and 9 converter

https://support.sas.com/content/dam/SAS/support/en/technical-papers/
record-layout-of-a-sas-version-8-or-9-data-set-in-sas-transport-format.pdf
'''
import sys, csv, struct, logging  # pylint: disable=multiple-imports
logging.basicConfig(level=logging.DEBUG if __debug__ else logging.INFO)

TESTVECTORS = {
    # from PDF referenced above
    'xpt': {
        1: b'\x41\x10\0\0\0\0\0\0',
        -1: b'\xc1\x10\0\0\0\0\0\0',
        0: b'\0\0\0\0\0\0\0\0',
        2: b'\x41\x20\0\0\0\0\0\0'},
    'ieee': {
        1: b'\0\0\0\0\0\0\xf0\x3f',
        -1: b'\0\0\0\0\0\0\xf0\xbf',
        0: b'\0\0\0\0\0\0\0\0',
        2: b'\0\0\0\0\0\0\0\x40'}
}

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

def ibm_to_double(bytestring, pack_output=False):
    '''
    convert 64-bit IBM float bytestring to IEEE floating point

    IBM:  seeeeeeemmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm
    IEEE: seeeeeeeeeeemmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm

    where s=sign bit, e=exponent bits, m=mantissa bits

    it doesn't map directly, though: IEEE uses a "biased" exponent, where
    the bias of 1023 is added to the actual exponent, whereas IBM's bias
    is 64.

    and the mantissa has an assumed 53rd bit of 1

    >>> ibm = TESTVECTORS['xpt']
    >>> ieee = TESTVECTORS['ieee']
    >>> [struct.unpack('<d', ieee[key])[0] for key in sorted(ieee)]
    [-1.0, 0.0, 1.0, 2.0]
    >>> [ibm_to_double(ibm[key]) for key in sorted(ibm)]
    [-1.0, 0.0, 1.0, 2.0]
    >>> {key: ibm_to_double(ibm[key], True) for key in ibm} == ieee
    True
    '''
    integer = struct.unpack('>Q', bytestring)[0]
    logging.debug('bytestring: %r, integer 0x%016x', bytestring, integer)
    sign, remainder = integer & (1 << 63), integer & ((1 << 63) - 1)
    exponent, mantissa = (remainder >> 56) - 64, remainder & ((1 << 56) - 1)
    logging.debug('exponent: 0x%04x, mantissa: 0x%012x', exponent, mantissa)
    repacked = struct.pack(
        '<Q',
        sign | ((exponent + 1023) << 52) | mantissa
    )
    logging.debug('sign 0x%016x, remainder 0x%016x, repacked %r',
                  sign, remainder, repacked)
    return repacked if pack_output else struct.unpack('<d', repacked)[0]

if __name__ == '__main__':
    xpt_to_csv(*sys.argv[1:])
