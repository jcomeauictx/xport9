#!/usr/bin/python3
'''
SAS .xpt (transport) versions 8 and 9 converter

written so as to support older formats as well, but not tested for those.

https://support.sas.com/content/dam/SAS/support/en/technical-papers/
record-layout-of-a-sas-version-8-or-9-data-set-in-sas-transport-format.pdf

note on encoding:
    "The SAS transport file should be read in a SAS session encoding
     that is compatible with the encoding used to create the file.
     There is no method of conveying encoding information other than
     documenting it with the delivery of the transport file."
'''
import sys, re, csv, struct, logging  # pylint: disable=multiple-imports
logging.basicConfig(level=logging.DEBUG if __debug__ else logging.INFO)

LIBRARY_HEADER = (rb'^HEADER RECORD\*{7}LIB[A-Z0-9]+ HEADER RECORD!{7}0{30} *$')

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
    csvdata = []
    state = 'awaiting_library_header'
    def get_library_header(record):
        pattern = re.compile(LIBRARY_HEADER)
        if pattern.match(record):
            logging.debug('found library header')
        else:
            raise ValueError('Invalid library header %r' % record)
        return 'awaiting_header'
    dispatch = {
        'awaiting_library_header': get_library_header,
    }

    while state != 'complete':
        logging.debug('state: %s', state)
        record = infile.read(80)
        if not record:
            logging.debug('conversion complete')
            state = 'complete'
            continue
        logging.debug('record: %r', record)
        state = dispatch[state](record)
    csvout.writerows(csvdata)

def ibm_to_double(bytestring, pack_output=False):
    '''
    convert 64-bit IBM float bytestring to IEEE floating point

    IBM:  seeeeeeemmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm
    IEEE: seeeeeeeeeeemmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm

    where s=sign bit, e=exponent bits, m=mantissa bits

    it doesn't map directly, though: IEEE uses a "biased" exponent, where
    the bias of 1023 is added to the actual exponent, whereas IBM's bias
    is 64.

    and the IEEE mantissa is normalized to a number between 1 and 2, and
    its representation has an assumed 53rd bit of 1; only the fractional
    bits are stored in the low 52 bits.

    whereas the IBM mantissa can be up to, but not including, 16.

    see https://stackoverflow.com/a/7141227/493161

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
    if integer == 0:
        return b'\0\0\0\0\0\0\0\0' if pack_output else 0.0
    sign = -1 if integer & 0x8000000000000000 else 1
    remainder = integer & 0x7fffffffffffffff
    logging.debug('sign %d, remainder 0x%016x', sign, remainder)
    exponent = (remainder >> 56) - 64
    mantissa = (remainder & ((1 << 56) - 1)) / float(1 << 52)
    logging.debug('exponent: 0x%04x, mantissa: %f', exponent, mantissa)
    double = sign * mantissa ** exponent
    logging.debug('double: %f', double)
    return struct.pack('d', double) if pack_output else double

if __name__ == '__main__':
    xpt_to_csv(*sys.argv[1:])
