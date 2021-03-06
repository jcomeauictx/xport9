#!/usr/bin/python3 -OO
# -*- coding: utf-8 -*-
'''
SAS .xpt (transport) versions 8 and 9 converter

written so as to support older formats as well, but not well tested for those.

*PRIMARILY* to support decoding of .xpt files from Pfizer/FDA.

https://support.sas.com/content/dam/SAS/support/en/technical-papers/
record-layout-of-a-sas-version-8-or-9-data-set-in-sas-transport-format.pdf

note on encoding:
    "The SAS transport file should be read in a SAS session encoding
     that is compatible with the encoding used to create the file.
     There is no method of conveying encoding information other than
     documenting it with the delivery of the transport file."

note on missing values:
    The documentation linked above gives a cryptic chart for missing values
    on page 7. It is explained better at https://support.sas.com/
    documentation/cdl/en/lrcon/62955/HTML/default/a002316433.htm:
    A missing numeric value is a single dot (char(0x2e)) followed by 7 nulls:
    ".\0\0\0\0\0\0\0"
    A missing character value is a single space followed by nulls: " \0\0..."
    A missing special format value is a character [A-Z_] (0x41 through 0x5a
    plus 0x5f representing the special value, followed by nulls,
    e.g. "B\0\0\0..."
    The SAS *source code* representation of the above is dot (.),
    space (' '), and dot-character .A, .B, ..., ._
'''
import sys, os, re, csv  # pylint: disable=multiple-imports
import struct, math, logging  # pylint: disable=multiple-imports
from datetime import datetime, timedelta
logging.basicConfig(level=logging.DEBUG if __debug__ else logging.INFO)

# python2 compatibility
try:
    unichr(42)  # pylint: disable=used-before-assignment
except NameError:
    # pylint: disable=invalid-name, redefined-builtin
    unichr = chr

try:
    unicode()
except NameError:
    # pylint: disable=invalid-name, redefined-builtin
    unicode = str

try:
    math.nan
except AttributeError:
    math.nan = 'nan'

try:
    csv.writer(open(os.devnull, 'w')).writerow([u'\u03bc'])
    PREPROCESS = lambda array: array
except UnicodeEncodeError:
    logging.warning('csv module cannot handle unicode, patching...')
    PREPROCESS = lambda array: [
        item.encode('utf8')
        if hasattr(item, 'encode') else item
        for item in array
    ]

if hasattr(sys.stdin, 'buffer'):
    # python3, sys.stdin.buffer is the bytes interface
    STDIN = sys.stdin.buffer  # pylint: disable=no-member
else:
    STDIN = sys.stdin

SAS_HEADER = lambda headertype, data: b''.join([
    b'^HEADER RECORD\\*{7}', headertype, b' +HEADER RECORD!{7}', data, b' *$'
])
SAS_EPOCH = datetime(1960, 1, 1)  # beginning of time in SAS
LIBRARY_HEADER = SAS_HEADER(b'LIB[A-Z0-9]+', b'0{30}')
REAL_HEADER = b'^(.{8})(.{8})(.{8})(.{8})(.{8}) {24}(.{16})$'
MEMBER_HEADER = SAS_HEADER(b'MEM[A-Z0-9]+', b'0{16}01600000000140')
DESCRIPTOR_HEADER = SAS_HEADER(b'DSC[A-Z0-9]+', b'0{30}')
# "The data following the DSCPTV8 record allows for a 32-character member name.
# "In the Version 6-styleformat, the member name was only 8 characters."
REAL_MEMBER_HEADER_6 = b'^(.{8})(.{8})(.{8})(.{8})(.{8}) {24}(.{16})$'
REAL_MEMBER_HEADER_8 = b'^(.{8})(.{32})(.{8})(.{8})(.{8})(.{16})$'
REAL_MEMBER_HEADER2 = b'^(.{16}) {16}(.{40})(.{8})$'
NAMESTR_HEADER = SAS_HEADER(b'NAM[A-Z0-9]+', b'0{6}([0-9]{6})0+')
OBSERVATION_HEADER = SAS_HEADER(b'OBS[A-Z0-9]*', b'0+')
NAMESTR = (
    # all 2-byte fields below are shorts except for nfill
    # the only other number is npos, which is a long
    # all the rest are character data or fill
    b'^(?P<ntype>.{2})'  # variable type, 1=numeric, 2=char
    b'(?P<nhfun>.{2})'   # hash of name (always 0)
    b'(?P<nlng>.{2})'    # length of variable in observation
    b'(?P<nvar0>.{2})'   # varnum (variable number)
    b'(?P<nname>.{8})'   # name of variable
    b'(?P<nlabel>.{40})' # label of variable
    b'(?P<nform>.{8})'   # name of format
    b'(?P<nfl>.{2})'     # format field length
    b'(?P<nfd>.{2})'     # format number of decimals
    b'(?P<nfj>.{2})'     # justification, 0=left, 1=right
    b'(?P<nfill>.{2})'   # unused, for alignment and future
    b'(?P<niform>.{8})'  # name of input format
    b'(?P<nifl>.{2})'    # informat length attribute
    b'(?P<nifd>.{2})'    # informat number of decimals
    b'(?P<npos>.{4})'    # position of value in observation
    b'(?P<longname>.{32})'  # long name for version 8 style labels
    b'(?P<lablen>.{2})'  # length of label
    b'(?P<rest>.{18})$'   # "remaining fields are irrelevant"
)
TESTVECTORS = {
    # from PDF referenced above
    'xpt': {
        1: b'\x41\x10\0\0\0\0\0\0',
        -1: b'\xc1\x10\0\0\0\0\0\0',
        0: b'\0\0\0\0\0\0\0\0',
        2: b'\x41\x20\0\0\0\0\0\0',
        # more to make sure needed bits aren't truncated
        3: b'\x41\x30\0\0\0\0\0\0',
    },
    'ieee': {
        1: b'\0\0\0\0\0\0\xf0\x3f',
        -1: b'\0\0\0\0\0\0\xf0\xbf',
        0: b'\0\0\0\0\0\0\0\0',
        2: b'\0\0\0\0\0\0\0\x40',
        3: b'\0\0\0\0\0\0\x08\x40',
    }
}
IBM = type('IBM', (), {
    'bits': 64,  # assuming double width floats as used by SAS
    'mantissa_bits': 56,
    'normalized': False,  # no guarantee of leading bit=1
    'implied_one_bit': False,  # no implied 57th bit=1
    'exponent_bits': 7,
    'exponent_multiplier': 4,  # every bump in exponent shifts another nybble
    'exponent_bias': 64,  # number added to exponent before packing
})
IEEE = type('IEEE', (), {
    'bits': 64,
    'mantissa_bits': 52,
    'normalized': True,  # mantissa shifted to mantissa_bits + 1
    'implied_one_bit': True,  # 52-bit mantissa has implied 53rd bit=1
    'exponent_bits': 11,
    'exponent_multiplier': 1,  # every bump in exponent shifts another bit
    'exponent_bias': 1023,
})
DOCUMENT = {
    'encoding': 'utf8',
}

def xpt_to_csv(filename=None, outfilename=None):
    '''
    convert xpt file to csv format
    '''
    # too many locals and statements can't be helped
    # pylint: disable=bad-option-value, too-many-locals, too-many-statements
    infile = open(filename, 'rb') if filename is not None else STDIN
    outfile = open(outfilename, 'w') if outfilename is not None else sys.stdout
    csvout = csv.writer(outfile)
    document = {'members': []}
    state = 'awaiting_library_header'
    def get_library_header(record):
        '''
        helper function to parse library header
        '''
        pattern = re.compile(LIBRARY_HEADER, re.DOTALL)
        if not pattern.match(record):
            raise ValueError('Invalid library header %r' % record)
        logging.debug('found library header')
        return 'awaiting_real_header'
    def get_real_header(record):
        '''
        helper function to parse "real" header
        '''
        pattern = re.compile(REAL_HEADER, re.DOTALL)
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
        '''
        helper function to parse modification time header
        '''
        document['modified'] = decode_sas_datetime(record.rstrip().decode())
        return 'awaiting_member_header'
    def get_member_header(record):
        '''
        helper function to parse member header
        '''
        pattern = re.compile(MEMBER_HEADER, re.DOTALL)
        match = pattern.match(record)
        if not match:
            raise ValueError('%r is not valid member header' % record)
        return 'awaiting_member_descriptor'
    def get_descriptor(record):
        '''
        helper function to parse descriptor
        '''
        pattern = re.compile(DESCRIPTOR_HEADER, re.DOTALL)
        match = pattern.match(record)
        if not match:
            raise ValueError('%r is not valid descriptor header' % record)
        return 'awaiting_member_data'
    def get_member_data(record, attempt=1):
        '''
        helper function to parse member data
        '''
        if attempt > 2:
            raise ValueError('%r not valid in old or new schema' % record)
        real_header = 'REAL_MEMBER_HEADER_%d' % document['real_version']
        logging.debug('assuming real member header is %s', real_header)
        pattern = re.compile(globals()[real_header], re.DOTALL)
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
            version = document['real_version']
            document['real_version'] = 8 if version == 6 else 6
            logging.warning('trying again with version %d',
                            document['real_version'])
            return get_member_data(record, attempt + 1)
        return 'awaiting_second_header'
    def get_second_header(record):
        '''
        helper function to parse member modification time and other attributes
        '''
        pattern = re.compile(REAL_MEMBER_HEADER2, re.DOTALL)
        match = pattern.match(record)
        if not match:
            raise ValueError('%r is not valid second header' % record)
        member = document['members'][-1]
        member['modified'] = decode_sas_datetime(match.group(1).decode())
        member['dataset_label'] = match.group(2).rstrip().decode()
        member['dataset_type'] = match.group(3).rstrip().decode()
        logging.debug('member: %s', member)
        # write out a header for the dataset
        csvout.writerow(PREPROCESS([
            '%s (%s)' % (member['dataset_name'], member['dataset_label']),
            'created %s' % member['created'],
            'modified %s' % member['modified'],
        ]))
        return 'awaiting_namestr_header'
    def get_namestr_header(record):
        '''
        helper function to parse namestr header
        '''
        pattern = re.compile(NAMESTR_HEADER, re.DOTALL)
        match = pattern.match(record)
        if not match:
            raise ValueError('%r is not valid namestr header' % record)
        logging.debug('unknown value in namestr header: %s', match.group(1))
        return 'awaiting_namestr_records'
    def get_namestr_records(record):
        '''
        helper function to parse namestr records (spreadsheet column headers)
        '''
        pattern = re.compile(OBSERVATION_HEADER, re.DOTALL)
        match = pattern.match(record)
        if not match:
            member = document['members'][-1]
            member['namestrings'] += record
            return 'awaiting_namestr_records'
        # now process each namestring
        pattern = re.compile(NAMESTR, re.DOTALL)
        member = document['members'][-1]
        for index in range(0, len(member['namestrings']), 140):
            namestring = member['namestrings'][index:index + 140]
            if len(namestring) < 140:
                logging.debug('discarding padding %r', namestring)
            else:
                match = pattern.match(namestring)
                if not match:
                    raise ValueError('pattern %s does not match %r' % (
                        pattern, namestring))
                member['names'].append(unpack_name(match.groupdict()))
        # write out column headers, short and long form
        csvout.writerow(PREPROCESS(
            [name['nname'] for name in member['names']]
        ))
        csvout.writerow(PREPROCESS(
            [name['nlabel'] for name in member['names']]
        ))
        last = member['names'][-1]
        member['recordlength'] = last['npos'] + last['nlng']
        return 'awaiting_observation_records'
    def get_observation_records(record):
        '''
        helper function to parse observation records (spreadsheet rows)
        '''
        pattern = re.compile(MEMBER_HEADER, re.DOTALL)
        match = pattern.match(record)
        member = document['members'][-1]
        recordlength = member['recordlength']
        if not match:
            member['observations'] += record
            if len(member['observations']) > recordlength:
                record = member['observations'][:recordlength]
                data = unpack_record(record, member['names'])
                member['observations'] = member['observations'][recordlength:]
                csvout.writerow(PREPROCESS(data))
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

def unpack_name(groupdict):
    '''
    unpack all the values from the regex match of a NAMESTR record
    '''
    for key, value in list(groupdict.items()):
        if key in ['nfill', 'rest'] or len(value) not in [2, 4]:
            groupdict[key] = value.rstrip(b'\0 ').decode()
        else:
            packformat = '>h' if len(value) == 2 else '>l'
            groupdict[key], = struct.unpack(packformat, value)
    logging.debug('groupdict: %s', groupdict)
    return groupdict

def unpack_record(rawdata, fields):
    '''
    unpack observation using namestr info as guide

    date and time formats explained at https://libguides.library.kent.edu/
    SAS/DatesTime
    '''
    data = []
    for field in fields:
        rawdatum = rawdata[field['npos']:field['npos'] + field['nlng']]
        decode_number = (ibm_to_double if not field['nform'] else
                         globals()['decode_%s' % field['nform'].lower()])
        data.append(
            (decode_number, decode_string)[field['ntype'] - 1](rawdatum)
        )
    return data

def decode_date(rawdatum):
    r'''
    SAS date values are stored internally as the number of days from 1960-01-01

    >>> decode_date(b'\x44\x56\x17\0\0\0\0\0')
    '2020-05-04'
    '''
    if rawdatum == b'.\0\0\0\0\0\0\0':
        date = None
    else:
        offset = ibm_to_double(rawdatum)
        date = str((SAS_EPOCH + timedelta(days=offset)).date())
    return date

def decode_time(rawdatum):
    r'''
    SAS time values are stored internally as the number of seconds
    since midnight

    >>> decode_time(b'\x44\xc8\xdc\0\0\0\0\0')
    '14:17:00'
    >>> decode_time(b'\x45\x10\x15\x80\0\0\0\0')
    '18:18:00'
    >>> decode_time(b'\x43\x3f\xc0\0\0\0\0\0')
    '00:17:00'
    '''
    if rawdatum in [b'.\0\0\0\0\0\0\0', b'\0\0\0\0\0\0\0\0']:
        time = None
    else:
        offset = ibm_to_double(rawdatum)
        time = str((SAS_EPOCH + timedelta(seconds=offset)).time())
    return time

def decode_datetime(rawdatum):
    r'''
    SAS datetime values are stored internally as the number of seconds
    since midnight 1960-01-01

    example: 0x4871801b5c000000, which is the IBM floating point
    representation of 1904221020 seconds.

    it yields 2020-05-04:14:17:00, which verifies the decode_time logic above,
    since both numbers were from the same document.

    >>> decode_datetime(b'\x48\x71\x80\x1b\x5c\0\0\0')
    '2020-05-04 14:17:00'
    '''
    if rawdatum == b'.\0\0\0\0\0\0\0':
        date_time = None
    else:
        offset = ibm_to_double(rawdatum)
        date_time = str(SAS_EPOCH + timedelta(seconds=offset))
    return date_time

def decode_string(string):
    r'''
    clean and decode string (character) data

    may need to try different encodings; utf-8 assumed at start

    >>> bytearray(decode_string(b'\0\0\0\0\0    '), 'utf8')
    bytearray(b'')
    >>> bytearray(decode_string(b'ABC 3(*ESC*){unicode 03BC}g'), 'utf8')
    bytearray(b'ABC 3\xce\xbcg')
    '''
    stripped = string.rstrip(b'\0 ')
    try:
        decoded = stripped.decode(DOCUMENT['encoding'])
    except UnicodeDecodeError:
        logging.warning('trying again assuming latin1 encoding')
        DOCUMENT['encoding'] = 'latin1'
        return decode_string(string)
    cleaned = re.sub(
        re.compile(r'\(\*ESC\*\)\{unicode ([0-9a-fA-F]+)\}'),
        lambda match: unichr(int(match.group(1), 16)),
        decoded
    )
    return cleaned

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
    r'''
    convert 64-bit IBM float bytestring to IEEE floating point

    IBM:  seeeeeeemmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm
    IEEE: seeeeeeeeeeemmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmmm

    where s=sign bit, e=exponent bits, m=mantissa bits

    it doesn't map directly, though: they both use a "biased" exponent.
    IEEE adds 1023 to the actual exponent, whereas IBM's bias is 64.

    the IBM exponent is base 16, i.e., a '1' shifts a nybble, as opposed to
    a single bit with the IEEE exponent.

    and the IEEE mantissa is normalized to a number between 1 and 2, and
    its representation has an assumed 53rd bit of 1; only the fractional
    bits are stored in the low 52 bits.

    whereas the IBM mantissa is all fractional, and any value 1 or above
    must have an exponent of 1 to shift the top nybble into integral
    territory. there can be up to 3 unused bits in the first nybble of
    the mantissa.

    >>> ibm = TESTVECTORS['xpt']
    >>> ieee = TESTVECTORS['ieee']
    >>> [struct.unpack('<d', ieee[key])[0] for key in sorted(ieee)]
    [-1.0, 0.0, 1.0, 2.0, 3.0]
    >>> [ibm_to_double(ibm[key]) for key in sorted(ibm)]
    [-1.0, 0.0, 1.0, 2.0, 3.0]
    >>> {key: ibm_to_double(ibm[key], True) for key in ibm} == ieee
    True
    >>> ibm_to_double(b'.\0\0\0\0\0\0\0')

    # check for warning for lost bits
    >>> ibm_to_double(b'\x41\x3f\xff\xff\xff\xff\xff\xff')
    3.9999999999999996
    '''
    check = bytestring.rstrip(b'\0')
    if len(check) <= 1:
        if not check:
            return bytestring if pack_output else 0.0
        return None if check == b'.' else math.nan
    # varname, = something  # is an easy way to unpack a one-element tuple.
    # I saw it while perusing the pypi xport code
    integer, = struct.unpack('>Q', bytestring)
    sign = integer & bitmask(IBM.bits - 1, reverse=True)
    remainder = integer & bitmask(IBM.bits - 1)
    exponent = (remainder >> IBM.mantissa_bits) - IBM.exponent_bias - 1
    mantissa = remainder & bitmask(IBM.mantissa_bits)
    # shift the high bit out to the left and chop it off for IEEE format
    shift = IBM.mantissa_bits - mantissa.bit_length() + 1
    mantissa = (mantissa << shift) & bitmask(IBM.mantissa_bits)
    exponent = (
        (exponent * IBM.exponent_multiplier)
        + (IBM.exponent_multiplier - shift)
        + IEEE.exponent_bias
    ) << IEEE.mantissa_bits
    if exponent.bit_length() > IBM.mantissa_bits + IBM.exponent_bits:
        raise FloatingPointError('Exponent %s too large' % exponent)
    bits_lost = IBM.mantissa_bits - IEEE.mantissa_bits
    if mantissa & bitmask(bits_lost):
        logging.warning('Losing low %d bits %s of %s', bits_lost,
                        bin(mantissa & bitmask(bits_lost)), bin(mantissa))
    mantissa >>= bits_lost
    repacked = struct.pack('>Q', sign | exponent | mantissa)
    sliced = slice(None) if sys.byteorder == 'big' else slice(None, None, -1)
    return repacked[sliced] if pack_output else struct.unpack('>d', repacked)[0]

def bitmask(bits, reverse=False):
    '''
    return bitmask for the given number of bits

    this means all binary ones, unless reverse=True, in which case only 1
    bit, the highest, is set, while `bits` bits are zeroed out

    >>> bitmask(3)
    7
    >>> bitmask(1)
    1
    >>> bitmask(0)
    0
    >>> bitmask(2, True)
    4
    >>> bitmask(4, True)
    16
    '''
    return 0 if bits < 1 else (1 << bits) - (not reverse)

if __name__ == '__main__':
    xpt_to_csv(*sys.argv[1:])
