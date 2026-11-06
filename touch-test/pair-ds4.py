#!/usr/bin/env python3
"""Pair a DualShock 4 controller over Bluetooth with a proper bond.

Run as the logged-in user.  BlueZ refuses HIDP input from devices that are
paired but not bonded (ClassicBondedOnly), which happens when the controller
is added through a desktop applet without an agent.

This script registers a real NoInputNoOutput agent, removes any stale entry,
scans, and retries ``pair`` until ``Bonded: yes`` appears in ``bluetoothctl info``.

Usage: pair-ds4.py MAC_ADDRESS [TIMEOUT_SECONDS]

After running, press the PS button on the controller to connect.
"""
import os
import pty
import re
import select
import sys
import time

MAC = sys.argv[1] if len(sys.argv) > 1 else "8C:41:F2:ED:FC:30"
TIMEOUT = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0


def read_for(fd, seconds):
    end = time.time() + seconds
    out = b""
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.2)
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    return out.decode(errors="replace")


def send(fd, command, wait=0.8):
    os.write(fd, (command + "\n").encode())
    return read_for(fd, wait)


def info(fd):
    text = send(fd, "info " + MAC, 1.5)
    paired = bool(re.search(r"Paired:\s+yes", text))
    bonded = bool(re.search(r"Bonded:\s+yes", text))
    connected = bool(re.search(r"Connected:\s+yes", text))
    return text, paired, bonded, connected


def main():
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp("bluetoothctl", ["bluetoothctl", "--agent",
                                   "NoInputNoOutput"])

    print("waiting for bluetoothctl agent", flush=True)
    read_for(fd, 1.5)
    print(send(fd, "default-agent", 1.0), end="", flush=True)
    print("removing stale entry", flush=True)
    print(send(fd, "remove " + MAC, 1.5), end="", flush=True)
    print("scanning; put the controller in pairing mode now", flush=True)
    send(fd, "scan on", 2.0)

    deadline = time.time() + TIMEOUT
    attempt = 0
    success = False
    while time.time() < deadline:
        attempt += 1
        print("pair attempt %d" % attempt, flush=True)
        out = send(fd, "pair " + MAC, 5.0)
        print(out, end="", flush=True)
        text, paired, bonded, connected = info(fd)
        print(text, end="", flush=True)
        if paired and bonded:
            success = True
            break
        time.sleep(1.0)

    if success:
        print(send(fd, "trust " + MAC, 1.0), end="", flush=True)
        print(send(fd, "scan off", 0.8), end="", flush=True)
        print("PAIRED_AND_BONDED", flush=True)
        print(info(fd)[0], end="", flush=True)
    else:
        print("PAIRING_TIMEOUT", flush=True)
        send(fd, "scan off", 0.8)

    send(fd, "quit", 0.5)
    try:
        os.close(fd)
    except OSError:
        pass
    os.waitpid(pid, 0)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())