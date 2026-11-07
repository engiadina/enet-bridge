#!/usr/bin/env python3
"""
Verbindung zur Bridge: lokaler serieller Port oder über SSH zum Rechner, an dem sie hängt.

  open_link("/dev/cu.usbserial-0001")              # lokal (pyserial)
  open_link("ssh://root@<ha-host>")           # HA-Rechner, Standard-Port der Bridge
  open_link("ssh://root@<ha-host>/dev/ttyUSB0")

Bei SSH läuft auf dem Raspi ein kleines Python-Skript, das stdin/stdout mit dem Port verbindet
(DTR/RTS aus). Das Öffnen startet die Bridge neu; open_link wartet auf das start-Ereignis.
Hält die HA-Integration den Port, schlägt das Öffnen fehl.
"""
import json, queue, subprocess, threading, time

DEFAULT_REMOTE_PORT = "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0"

REMOTE_SCRIPT = r'''
import sys, threading, serial
s = serial.Serial(); s.port = sys.argv[1]; s.baudrate = 115200; s.timeout = 0.2
s.dtr = False; s.rts = False; s.open()
def rx():
    buf = b""
    while True:
        buf += s.read(s.in_waiting or 1)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            sys.stdout.buffer.write(line.rstrip(b"\r") + b"\n"); sys.stdout.flush()
threading.Thread(target=rx, daemon=True).start()
for line in sys.stdin.buffer:
    s.write(line)
'''


class Link:
    def __init__(self, reader, writer, closer):
        self._write, self._close = writer, closer
        self._q = queue.Queue()
        threading.Thread(target=self._pump, args=(reader,), daemon=True).start()

    def _pump(self, reader):
        for line in reader:
            self._q.put(line.decode("utf-8", "replace").rstrip("\r\n"))
        self._q.put(None)

    def write_line(self, text):
        self._write((text + "\n").encode())

    def send(self, obj):
        self.write_line(json.dumps(obj, separators=(",", ":")))

    def readline(self, timeout=None):
        """Nächste Zeile (str) oder None bei Zeitüberschreitung/Ende."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def read_json(self, timeout=None):
        """Nächste JSON-Nachricht oder None; Nicht-JSON-Zeilen (Bootmeldungen) werden übersprungen."""
        end = time.time() + (timeout if timeout is not None else 1e9)
        while True:
            line = self.readline(max(0.0, end - time.time()))
            if line is None:
                return None
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    pass

    def wait(self, pred, timeout):
        end = time.time() + timeout
        while time.time() < end:
            m = self.read_json(end - time.time())
            if m is not None and pred(m):
                return m
        return None

    def close(self):
        self._close()


def open_link(target, wait_start=True):
    if target.startswith("ssh://"):
        rest = target[6:]
        host, _, port = rest.partition("/")
        port = "/" + port if port else DEFAULT_REMOTE_PORT
        p = subprocess.Popen(["ssh", "-o", "BatchMode=yes", host, "python3", "-u", "-c",
                              f"'{REMOTE_SCRIPT}'", port],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)

        def write(b):
            p.stdin.write(b)
            p.stdin.flush()
        link = Link(p.stdout, write, p.terminate)
    else:
        import serial
        s = serial.Serial()
        s.port, s.baudrate, s.timeout = target, 115200, 0.2
        s.dtr = s.rts = False
        s.open()

        def reader():
            buf = b""
            while s.is_open:
                buf += s.read(s.in_waiting or 1)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield line + b"\n"
        link = Link(reader(), s.write, s.close)
    if wait_start:
        link.wait(lambda m: m.get("ev") == "start", 4.0)
    return link
