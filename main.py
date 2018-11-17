#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import sys
import serial
import socket
import threading

import configparser
from pathlib import Path

cfPath = str(Path.home().joinpath('.config/x5105/config'))

cf = configparser.ConfigParser()
cf.read(cfPath)

DEFAULT_RIG_FILE = cf.get('rig', 'file', fallback = '/dev/ttyUSB0')
DEFAULT_BAUDRATE = cf.getint('rig', 'baudrate', fallback = 19200)
DEFAULT_RIG_TIMEOUT = cf.getint('rig', 'timeout', fallback = 1)

DEFAULT_BIND_ADDRESS = (
    cf.get('daemon', 'host', fallback = '127.0.0.1'),
    cf.getint('daemon', 'port', fallback = 4532))
BACKLOG = 1

LOG_LEVEL = logging.getLevelName(cf.get('logging', 'level', fallback = 'INFO'))

logging.basicConfig(level = LOG_LEVEL,
    format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s')

logger = logging.getLogger(name = 'APP')
netctl_logger = logging.getLogger(name = 'NET')
rigctl_logger = logging.getLogger(name = 'RIG')

logger.setLevel(LOG_LEVEL)
netctl_logger.setLevel(LOG_LEVEL)
rigctl_logger.setLevel(LOG_LEVEL)

# from IC-7000 dump_state output, changed the ITU region from 1 to 0.
NET_DUMP_STATE = b"""0
360
0
30000.000000 199999999.000000 0x1ff -1 -1 0x10000003 0x0
400000000.000000 470000000.000000 0x1ff -1 -1 0x10000003 0x0
0 0 0 0 0 0 0
1800000.000000 2000000.000000 0x1be 2000 100000 0x10000003 0x1
3500000.000000 4000000.000000 0x1be 2000 100000 0x10000003 0x1
7000000.000000 7300000.000000 0x1be 2000 100000 0x10000003 0x1
10100000.000000 10150000.000000 0x1be 2000 100000 0x10000003 0x1
14000000.000000 14350000.000000 0x1be 2000 100000 0x10000003 0x1
18068000.000000 18168000.000000 0x1be 2000 100000 0x10000003 0x1
21000000.000000 21450000.000000 0x1be 2000 100000 0x10000003 0x1
24890000.000000 24990000.000000 0x1be 2000 100000 0x10000003 0x1
28000000.000000 29700000.000000 0x1be 2000 100000 0x10000003 0x1
50000000.000000 54000000.000000 0x1be 2000 100000 0x10000003 0x1
144000000.000000 148000000.000000 0x1be 2000 50000 0x10000003 0x2
430000000.000000 440000000.000000 0x1be 2000 35000 0x10000003 0x2
1800000.000000 2000000.000000 0x1 1000 40000 0x10000003 0x1
3500000.000000 4000000.000000 0x1 1000 40000 0x10000003 0x1
7000000.000000 7300000.000000 0x1 1000 40000 0x10000003 0x1
10100000.000000 10150000.000000 0x1 1000 40000 0x10000003 0x1
14000000.000000 14350000.000000 0x1 1000 40000 0x10000003 0x1
18068000.000000 18168000.000000 0x1 1000 40000 0x10000003 0x1
21000000.000000 21450000.000000 0x1 1000 40000 0x10000003 0x1
24890000.000000 24990000.000000 0x1 1000 40000 0x10000003 0x1
28000000.000000 29700000.000000 0x1 1000 40000 0x10000003 0x1
50000000.000000 54000000.000000 0x1 1000 40000 0x10000003 0x1
144000000.000000 148000000.000000 0x1 2000 20000 0x10000003 0x2
430000000.000000 440000000.000000 0x1be 2000 14000 0x10000003 0x2
0 0 0 0 0 0 0
0x19e 1
0x61 10
0x1ff 100
0x1ff 1000
0x1ff 5000
0x1ff 9000
0x1ff 10000
0x1ff 12500
0x1ff 20000
0x1ff 25000
0x1ff 100000
0x61 1000000
0 0
0xc 2400
0xc 1800
0xc 3000
0x20 10000
0x20 15000
0x20 7000
0x192 500
0x192 250
0x82 1200
0x110 2400
0x1 6000
0x1 3000
0x1 9000
0x40 280000
0 0
9999
9999
0
0
10 
12 
0x5b3ff
0x5b3ff
0x7427ff3f
0x27ff3f
0x37
0x37
"""


ser = None
daemon_thread = None
should_exit = False

def open_rig(rig_file = DEFAULT_RIG_FILE, baudrate = DEFAULT_BAUDRATE, timeout = DEFAULT_RIG_TIMEOUT):
    global ser
    ser = serial.Serial(rig_file, baudrate, timeout = timeout)
    rigctl_logger.info('Rig is opened: %s', rig_file)


def close_rig():
    global ser
    if ser:
        ser.close()
        rigctl_logger.info('Rig is closed.')
        ser = None


def output_bytes(lb, bs):
    out_str = ' '.join('%02X' % i for i in bs)
    rigctl_logger.debug('%s: %s', lb, out_str)


def exec_cmd(cmd):
    global ser

    req = b'\xfe\xfe\x70\xe0%s\xfd' % cmd
    output_bytes('REQ ', req)
    ser.write(req)

    echo = ser.read(len(req))

    resp = b''
    while True:
        c = ser.read()
        resp = resp + c
        if c == b'\xfd':
            break

    output_bytes('RESP', echo + resp)

    return resp


def resp_is_ok(resp):
    return len(resp) == 6 and resp[4] == 0xfb


def rig_get_mode_and_filter():
    result = None

    resp = exec_cmd(b'\04')

    if len(resp) == 8:
        result = (
            {
                0x00: b'LSB',
                0x01: b'USB',
                0x02: b'AM',
                0x03: b'CW',
                0x05: b'FM',
                0x07: b'CWR'
            }.get(resp[-3], b'USB'),
            {
                0x01: 6000,
                0x02: 2400,
                0x03: 500
            }.get(resp[-2], 2400))

    return result


def rig_set_mode(mode):

    m = {
        b'LSB': b'\x00\x02',
        b'USB': b'\x01\x02',
        b'AM' : b'\x02\x01',
        b'CW' : b'\x03\x03',
        b'FM' : b'\x05\x01',
        b'CWR': b'\x07\x03'
    }.get(mode, b'\x01\x02')

    resp = exec_cmd(b'\x06%s' % m)

    return resp_is_ok(resp)


def rig_get_ptt():
    result = None

    resp = exec_cmd(b'\x1c\x00')

    if len(resp) == 8:
        result = resp[-2] != 0
    
    return result


def rig_set_ptt(ptt):
    resp = exec_cmd(b'\x1c\x00%s' % (b'\01' if ptt else b'\00'))

    return resp_is_ok(resp)


def rig_get_freq():
    result = None

    resp = exec_cmd(b'\x03')

    if len(resp) == 11:
        result = 0
        for i in range(-2, -7, -1):
            b = resp[i]
            result = result * 100 + (b & 0x0F) + ((b >> 4) & 0x0F) * 10
    
    return result


def rig_set_freq(freq):
    freq_str = '%010d' % freq
    cmd = b'\x05%s' % bytes.fromhex(freq_str[8:10] + freq_str[6:8] + freq_str[4:6] + freq_str[2:4] + freq_str[0:2])
    resp = exec_cmd(cmd)

    return resp_is_ok(resp)


def sock_readline(sock):
    result = b''

    while True:
        rd = sock.recv(1024)
        if not rd:
            return None
        
        result = result + rd
        if result[-1] == 0x0a:
            break

    return result


def tcplink(sock, addr):
    global daemon_thread
    global should_exit

    client = '%s:%s' % addr
    msg_fmt = '[%s] %%s' % client

    netctl_logger.info('Accept new connection from %s...' % client)

    try:
        ok = b'RPRT 0\n'

        while not should_exit:
            rdata = sock_readline(sock)
            if not rdata:
                break

            netctl_logger.debug(msg_fmt % '<<<<<<<< RECEIVED >>>>>>>>')
            netctl_logger.debug(msg_fmt % rdata)

            for data in rdata.splitlines():
                resp = b'RPRT -11\n'
                netctl_logger.debug(msg_fmt % '======== EXEC_CMD ========')
                netctl_logger.debug(msg_fmt % data)
                if data == b'q': # quit
                    resp = None
                    break
                elif data == b'v': # get_vfo - not supported
                    pass
                elif data == b'f':
                    freq = rig_get_freq()
                    if (freq):
                        resp = b'%d\n' % freq
                elif data == b'm':
                    r = rig_get_mode_and_filter()
                    if (r):
                        resp = b'%s\n%d\n' % r
                elif data[0] == b'M'[0]:
                    mode = data.split(b' ')[1]
                    if rig_set_mode(mode):
                        resp = ok
                elif data[0] == b'V'[0]: # set_vfo - not supported
                    resp = ok
                elif data[0] == b'F'[0]:
                    freq = int(float(data.split(b' ')[1]))
                    if rig_set_freq(freq):
                        resp = ok
                elif data == b's': # get_split_info - not supported
                    pass
                elif data[0] == b'S'[0]: # set_split - not supported
                    resp = ok
                elif data == b't':
                    resp = (b'1\n' if rig_get_ptt() else b'0\n')
                elif data[0] == b'T'[0]:
                    if rig_set_ptt(data.strip().split(b' ')[1] != b'0'):
                        resp = ok
                elif data == b'\\dump_state':
                    resp = NET_DUMP_STATE

                if resp:
                    netctl_logger.debug(msg_fmt % resp)
                    sock.sendall(resp)
                else:
                    break

    except Exception as e:
        netctl_logger.error(e)
    finally:
        sock.close()
        netctl_logger.info('Connection closed: %s.' % client)
        daemon_thread = None


def start_server(bind_address = DEFAULT_BIND_ADDRESS):
    global daemon_thread
    global should_exit

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(bind_address)
    s.listen(BACKLOG)

    netctl_logger.info('Server is started at %s:%s...' % bind_address)

    try:
        while True:
            sock, addr = s.accept()

            if not daemon_thread:
                daemon_thread = threading.Thread(target = tcplink, args = (sock, addr))
                daemon_thread.start()
            else:
                sock.close()
                netctl_logger.warning('Connection is rejected: %s:%s' % addr)
                netctl_logger.warning('Only one connection can be accepted.')
    except KeyboardInterrupt:
        should_exit = True
        if daemon_thread:
            daemon_thread.join()

        raise
    finally:
        s.close()
        netctl_logger.info('Server is stopped.')


def main():
    try:
        open_rig()
        start_server()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(e)
        exit(1)
    finally:
        close_rig()


if __name__ == '__main__':
    main()
