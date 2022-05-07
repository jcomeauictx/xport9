#!/usr/bin/python3 -OO
'''
SAS .xpt (transport) versions 8 and 9 converter

written so as to support older formats as well, but not well tested for those.

*PRIMARILY* to support decoding of possibly obfuscated .xpt files from
Pfizer/FDA. look at decode_time() to see what I'm talking about. so, this
may not work with "normal" SAS files.

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
    A missing special format value is a dot followed by the letter A-Z
    representing the special value, followed by nulls, e.g. ".B\0\0\0..."
'''
import sys, os, re, csv  # pylint: disable=multiple-imports
import struct, math, logging  # pylint: disable=multiple-imports
from datetime import datetime, timedelta
logging.basicConfig(level=logging.DEBUG if __debug__ else logging.INFO)

SAS_EPOCH = datetime(1960, 1, 1)  # beginning of time in SAS
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
NAMESTR = (
    # all 2-byte fields below are shorts except for nfill
    # the only other number is npos, which is a long
    # all the rest are character data or fill
    rb'^(?P<ntype>.{2})'  # variable type, 1=numeric, 2=char
    rb'(?P<nhfun>.{2})'   # hash of name (always 0)
    rb'(?P<nlng>.{2})'    # length of variable in observation
    rb'(?P<nvar0>.{2})'   # varnum (variable number)
    rb'(?P<nname>.{8})'   # name of variable
    rb'(?P<nlabel>.{40})' # label of variable
    rb'(?P<nform>.{8})'   # name of format
    rb'(?P<nfl>.{2})'     # format field length
    rb'(?P<nfd>.{2})'     # format number of decimals
    rb'(?P<nfj>.{2})'     # justification, 0=left, 1=right
    rb'(?P<nfill>.{2})'   # unused, for alignment and future
    rb'(?P<niform>.{8})'  # name of input format
    rb'(?P<nifl>.{2})'    # informat length attribute
    rb'(?P<nifd>.{2})'    # informat number of decimals
    rb'(?P<npos>.{4})'    # position of value in observation
    rb'(?P<longname>.{32})'  # long name for version 8 style labels
    rb'(?P<lablen>.{2})'  # length of label
    rb'(?P<rest>.{18})$'   # "remaining fields are irrelevant"
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
    document = {'members': []}
    state = 'awaiting_library_header'
    def get_library_header(record):
        pattern = re.compile(LIBRARY_HEADER, re.DOTALL)
        if not pattern.match(record):
            raise ValueError('Invalid library header %r' % record)
        logging.debug('found library header')
        return 'awaiting_real_header'
    def get_real_header(record):
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
        document['modified'] = decode_sas_datetime(record.rstrip().decode())
        return 'awaiting_member_header'
    def get_member_header(record):
        pattern = re.compile(MEMBER_HEADER, re.DOTALL)
        match = pattern.match(record)
        if not match:
            raise ValueError('%r is not valid member header' % record)
        return 'awaiting_member_descriptor'
    def get_descriptor(record):
        pattern = re.compile(DESCRIPTOR_HEADER, re.DOTALL)
        match = pattern.match(record)
        if not match:
            raise ValueError('%r is not valid descriptor header' % record)
        return 'awaiting_member_data'
    def get_member_data(record, attempt=1):
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
            document['real_version'] = (6, 8)[document['real_version'] == 6]
            logging.warning('trying again with version %d',
                            document['real_version'])
            return get_member_data(record, attempt + 1)
        return 'awaiting_second_header'
    def get_second_header(record):
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
        csvout.writerow([
            '%s (%s)' % (member['dataset_name'], member['dataset_label']),
            'created %s' % member['created'],
            'modified %s' % member['modified'],
        ])
        return 'awaiting_namestr_header'
    def get_namestr_header(record):
        pattern = re.compile(NAMESTR_HEADER, re.DOTALL)
        match = pattern.match(record)
        if not match:
            raise ValueError('%r is not valid namestr header' % record)
        logging.debug('unknown value in namestr header: %s', match.group(1))
        return 'awaiting_namestr_records'
    def get_namestr_records(record):
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
        csvout.writerow([name['nname'] for name in member['names']])
        csvout.writerow([name['nlabel'] for name in member['names']])
        last = member['names'][-1]
        member['recordlength'] = last['npos'] + last['nlng']
        return 'awaiting_observation_records'
    def get_observation_records(record):
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
                csvout.writerow(data)
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
            groupdict[key] = struct.unpack(packformat, value)[0]
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

    but what was found experimentally is that, of for example
    0x4456170000000000, 0x5617 is the number of days; the meaning of 0x44 in
    the first byte is as yet undetermined.

    >>> decode_date(b'\x44\x56\x17\0\0\0\0\0')
    '2020-05-04'
    '''
    if rawdatum[0] == 0x44 and rawdatum[3:] == b'\0\0\0\0\0':
        offset = struct.unpack('>H', rawdatum[1:3])[0]
        date = str((SAS_EPOCH + timedelta(days=offset)).date())
    elif rawdatum == b'.\0\0\0\0\0\0\0':
        date = None
    else:
        raise ValueError('Unknown DATE representation %r' % rawdatum)
    if os.getenv('DEBUG_DATETIMES') and date is not None:
        date += (' (DATE %s)' % rawdatum.hex())
    return date

def decode_time(rawdatum):
    r'''
    SAS time values are stored internally as the number of seconds
    since midnight

    example is 0x44c8dc0000000000. but an offset of 0xffff is only 6:12:15 PM,
    so there are other formats in use.

    >>> decode_time(b'\x44\xc8\xdc\0\0\0\0\0')
    '14:17:00'
    >>> decode_time(b'\x45\x10\x15\x80\0\0\0\0')
    '18:18:00'
    >>> decode_time(b'\x43\x3f\xc0\0\0\0\0\0')
    '00:17:00'
    '''
    if rawdatum[0] == 0x43 and rawdatum[3:] == b'\0\0\0\0\0':
        modified = rawdatum[1:3]
        offset = struct.unpack('>H', modified)[0] / 16
        time = str((SAS_EPOCH + timedelta(seconds=offset)).time())
    elif rawdatum[0] == 0x44 and rawdatum[3:] == b'\0\0\0\0\0':
        modified = rawdatum[1:3]
        offset = struct.unpack('>H', modified)[0]
        time = str((SAS_EPOCH + timedelta(seconds=offset)).time())
    elif rawdatum[0] == 0x45 and rawdatum[4:] == b'\0\0\0\0':
        modified = b'\0' + rawdatum[1:4]
        offset = struct.unpack('>L', modified)[0] >> 4
        time = str((SAS_EPOCH + timedelta(seconds=offset)).time())
    elif rawdatum in [b'.\0\0\0\0\0\0\0', b'\0\0\0\0\0\0\0\0']:
        time = None
    else:
        raise ValueError('Unknown TIME representation %r' % rawdatum)
    if os.getenv('DEBUG_DATETIMES') and time is not None:
        time += (' (TIME %s)' % rawdatum.hex())
    return time

def decode_datetime(rawdatum):
    r'''
    SAS datetime values are stored internally as the number of seconds
    since midnight 1960-01-01

    example: 0x4871801b5c000000, the 0x71791b5c is the seconds offset

    it yields 2020-05-04:14:17:00, which verifies the decode_time logic above,
    since both numbers were from the same document.

    >>> decode_datetime(b'\x48\x71\x80\x1b\x5c\0\0\0')
    '2020-05-04 14:17:00'
    '''
    if rawdatum[0] == 0x48 and rawdatum[5:] == b'\0\0\0':
        offset = struct.unpack('>L', rawdatum[1:5])[0]
        date_time = str(SAS_EPOCH + timedelta(seconds=offset))
    elif rawdatum == b'.\0\0\0\0\0\0\0':
        date_time = None
    else:
        raise ValueError('Unknown DATETIME representation %r' % rawdatum)
    if os.getenv('DEBUG_DATETIMES') and date_time is not None:
        date_time += ' (DATETIME %s)' % rawdatum.hex()
    return date_time

def decode_string(string):
    r'''
    clean and decode string (character) data

    may need to try different encodings, but for now assume utf8

    >>> decode_string(b'\0\0\0\0\0    ')
    ''
    >>> decode_string(b'ABC 3(*ESC*){unicode 03BC}g')
    'ABC 3μg'
    '''
    decoded = string.rstrip(b'\0 ').decode()
    cleaned = re.sub(
        re.compile(r'\(\*ESC\*\)\{unicode ([0-9a-fA-F]+)\}'),
        lambda match: chr(int(match.group(1), 16)),
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
    >>> ibm_to_double(b'.\0\0\0\0\0\0\0')
    '''
    if bytestring == b'.\0\0\0\0\0\0\0':  # missing numeric value
        return None
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
    try:
        double = sign * mantissa ** exponent
        logging.debug('double: %f', double)
        return struct.pack('d', double) if pack_output else double
    except ZeroDivisionError:
        logging.error('cannot convert sign %d, mantissa %f, and exponent %d'
                      ' to float', sign, mantissa, exponent)
        return math.nan

if __name__ == '__main__':
    xpt_to_csv(*sys.argv[1:])
