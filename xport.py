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
from datetime import datetime
logging.basicConfig(level=logging.DEBUG if __debug__ else logging.INFO)

LIBRARY_HEADER = rb'^HEADER RECORD\*{7}LIB[A-Z0-9]+ HEADER RECORD!{7}0{30} *$'
REAL_HEADER = rb'^(.{8})(.{8})(.{8})(.{8})(.{8}) {24}(.{16})$'
MEMBER_HEADER = (
    rb'^HEADER RECORD\*{7}MEM[A-Z0-9]+ +HEADER RECORD!{7}0{16}01600000000140 *$'
)
DESCRIPTOR_HEADER = (
    rb'^HEADER RECORD\*{7}DSC[A-Z0-9]+ +HEADER RECORD!{7}0{30} *$'
)
# "The data following the DSCPTV8 record allows for a 32-character member name.
# "In the Version 6-styleformat, the member name was only 8 characters."
REAL_MEMBER_HEADER_6 = rb'^(.{8})(.{8})(.{8})(.{8})(.{8}) {24}(.{16})$'
REAL_MEMBER_HEADER_8 = rb'^(.{8})(.{32})(.{8})(.{8})(.{8})(.{16})$'
REAL_MEMBER_HEADER2 = rb'^(.{16}) {16}(.{40})(.{8})$'
NAMESTR_HEADER = (
    rb'^HEADER RECORD\*{7}NAM[A-Z0-9]+ +HEADER +RECORD!{7}0{6}([0-9]{6})0+ *$'
)
OBSERVATION_HEADER = (
    rb'HEADER RECORD\*{7}OBS[A-Z0-9]* +HEADER +RECORD!{7}0+ *$'
)
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
    # pylint: disable=too-many-locals, too-many-statements  # can't be helped
    infile = open(filename, 'rb') if filename is not None else sys.stdin
    outfile = open(outfilename, 'w') if outfilename is not None else sys.stdout
    csvout = csv.writer(outfile)
    csvdata = []
    document = {'members': []}
    state = 'awaiting_library_header'
    def get_library_header(record):
        pattern = re.compile(LIBRARY_HEADER)
        if not pattern.match(record):
            raise ValueError('Invalid library header %r' % record)
        logging.debug('found library header')
        return 'awaiting_real_header'
    def get_real_header(record):
        pattern = re.compile(REAL_HEADER)
        match = pattern.match(record)
        if not match:
            raise ValueError('Not finding valid header in %r' % record)
        assert match.group(1).rstrip().decode() == 'SAS'
        assert match.group(2).rstrip().decode() == 'SAS'
        document['sas_version'] = match.group(4).rstrip().decode()
        document['real_version'] = 8  # assume v8 or v9 for now
        document['os'] = match.group(5).rstrip(b'\0 ').decode()
        document['created'] = decode_sas_datetime(match.group(6).decode())
        logging.debug('document: %s', document)
        assert document['sas_version'] and document['os']
        return 'awaiting_mtime_header'
    def get_mtime_header(record):
        document['modified'] = decode_sas_datetime(record.rstrip().decode())
        return 'awaiting_member_header'
    def get_member_header(record):
        pattern = re.compile(MEMBER_HEADER)
        match = pattern.match(record)
        if not match:
            raise ValueError('%r is not valid member header' % record)
        return 'awaiting_member_descriptor'
    def get_descriptor(record):
        pattern = re.compile(DESCRIPTOR_HEADER)
        match = pattern.match(record)
        if not match:
            raise ValueError('%r is not valid descriptor header' % record)
        return 'awaiting_member_data'
    def get_member_data(record, attempt=1):
        if attempt > 2:
            raise ValueError('%r not valid in old or new schema' % record)
        real_header = 'REAL_MEMBER_HEADER_%d' % document['real_version']
        logging.debug('assuming real member header is %s', real_header)
        pattern = re.compile(globals()[real_header])
        match = pattern.match(record)
        if not match:
            raise ValueError('%r is not valid real member header' % record)
        assert match.group(1).rstrip().decode() == 'SAS'
        document['members'].append({
            'dataset_name': match.group(2).rstrip().decode(),
            'namestrings': b'',
            'names': [],
            'observations': b'',
            'data': [],
        })
        member = document['members'][-1]
        member['sas_version'] = match.group(4).rstrip().decode()
        member['os'] = match.group(5).rstrip(b'\0 ').decode()
        member['created'] = decode_sas_datetime(match.group(6).decode())
        logging.debug('member: %s', member)
        if not (member['sas_version'] and member['os']):
            # assume wrong "real" version, and switch
            document['real_version'] = (6, 8)[document['real_version'] == 6]
            logging.warning('trying again with version %d',
                            document['real_version'])
            return get_member_data(record, attempt + 1)
        return 'awaiting_second_header'
    def get_second_header(record):
        pattern = re.compile(REAL_MEMBER_HEADER2)
        match = pattern.match(record)
        if not match:
            raise ValueError('%r is not valid second header' % record)
        member = document['members'][-1]
        member['modified'] = decode_sas_datetime(match.group(1).decode())
        member['dataset_label'] = match.group(2).rstrip().decode()
        member['dataset_type'] = match.group(3).rstrip().decode()
        logging.debug('member: %s', member)
        return 'awaiting_namestr_header'
    def get_namestr_header(record):
        pattern = re.compile(NAMESTR_HEADER)
        match = pattern.match(record)
        if not match:
            raise ValueError('%r is not valid namestr header' % record)
        logging.debug('unknown value in namestr header: %s', match.group(1))
        return 'awaiting_namestr_records'
    def get_namestr_records(record):
        pattern = re.compile(OBSERVATION_HEADER)
        match = pattern.match(record)
        if not match:
            member = document['members'][-1]
            member['namestrings'] += record
            return 'awaiting_namestr_records'
        return 'awaiting_observation_records'
    def get_observation_records(record):
        pattern = re.compile(MEMBER_HEADER)
        match = pattern.match(record)
        if not match:
            member = document['members'][-1]
            member['observations'] += record
            return 'awaiting_observation_records'
        return get_member_header(record)

    dispatch = {
        'awaiting_library_header': get_library_header,
        'awaiting_real_header': get_real_header,
        'awaiting_mtime_header': get_mtime_header,
        'awaiting_member_header': get_member_header,
        'awaiting_member_descriptor': get_descriptor,
        'awaiting_member_data': get_member_data,
        'awaiting_second_header': get_second_header,
        'awaiting_namestr_header': get_namestr_header,
        'awaiting_namestr_records': get_namestr_records,
        'awaiting_observation_records': get_observation_records,
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

def decode_sas_datetime(datestring):
    '''
    decode 16-byte datetime format and return as datetime object

    from the referenced PDF: "Note thatonly a 2-digit year appears.
    If any programneedsto readin this 2-digit year, be prepared to deal
    with dates in the 1900s or the 2000s."

    I'm letting the datetime module handle this. It seems to be crossing into
    the past on January 1, 1969.

    >>> decode_sas_datetime('31DEC68:23:59:59')
    datetime.datetime(2068, 12, 31, 23, 59, 59)
    >>> decode_sas_datetime('01JAN69:00:00:00')
    datetime.datetime(1969, 1, 1, 0, 0)
    '''
    return datetime.strptime(datestring, '%d%b%y:%H:%M:%S')

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
