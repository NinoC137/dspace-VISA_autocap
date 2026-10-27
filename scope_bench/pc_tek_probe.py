#!/usr/bin/env python3
"""Probe the Tektronix MSO58B socket server directly from the PC (no Pi in the path)."""
import socket
import time

HOST, PORT = "192.168.2.200", 4000

t0 = time.time()
s = socket.socket()
s.settimeout(6)
s.connect((HOST, PORT))
print("connect      : %.3f s" % (time.time() - t0))

s.sendall(b"*IDN?\n")
print("IDN          : %s" % s.recv(4096).decode(errors="replace").strip())

s.sendall(b"HORizontal:RECOrdlength?\n")
print("RECOrdlength : %s" % s.recv(4096).decode(errors="replace").strip())

# 小规模二进制读，验证 PC 侧也能解析 IEEE 定长块
s.sendall(b"DATa:SOUrce CH1\n")
s.sendall(b"DATa:ENCdg RIBinary\n")
s.sendall(b"DATa:WIDth 2\n")
s.sendall(b"DATa:STARt 1\n")
s.sendall(b"DATa:STOP 5000\n")
t0 = time.time()
s.sendall(b"CURVe?\n")
hdr = b""
while len(hdr) < 2:
    hdr += s.recv(2 - len(hdr))
nd = int(chr(hdr[1]))
while len(hdr) < 2 + nd:
    hdr += s.recv(2 + nd - len(hdr))
n = int(hdr[2:2 + nd].decode())
body = b""
while len(body) < n:
    c = s.recv(min(65536, n - len(body)))
    if not c:
        break
    body += c
print("CURVe?       : %d bytes in %.3f s (block header %r)" % (len(body), time.time() - t0, hdr))

import struct
vals = struct.unpack(">%dh" % (len(body) // 2), body)
print("             : %d points, first 5 = %s" % (len(vals), list(vals[:5])))

s.sendall(b"ALLEv?\n")
print("ALLEv?       : %s" % s.recv(4096).decode(errors="replace").strip()[:150])
s.close()
print("OK: PC can talk to the scope directly")
