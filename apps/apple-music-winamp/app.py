#!/usr/bin/env python3
"""Winamp-inspired Apple Music display and hardware controls (macOS).

python3 app.py --host 10.0.4.20
python3 app.py --host 127.0.0.1:8321  # BUSY Bar Manager
python3 app.py --demo --host 127.0.0.1:8080  # animated preview, no Music needed

MIT License, Copyright (c) 2026 Nachenko.
Visualizer motion is decorative. No screen, microphone, or system audio capture.
"""
import argparse
import base64
import hashlib
import json
import math
import os
import queue
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

APP = 'apple-music-winamp'
SCRIPT = '''on packed(values)
 set AppleScript's text item delimiters to ASCII character 30
 return values as text
end packed
on run argv
 if application "Music" is not running then return my packed({"closed", "Open Music", "", "0", "0"})
 tell application "Music"
  set operation to item 1 of argv
  if operation is "toggle" then
   playpause
  else if operation is "previous" then
   previous track
  else if operation is "next" then
   next track
  end if
  set playbackState to player state as text
  if playbackState is "stopped" then return my packed({playbackState, "Apple Music", "Choose a song", "0", "0"})
  return my packed({playbackState, name of current track, artist of current track, player position as text, duration of current track as text})
 end tell
end run'''
SMALL_ADVANCES = [2, 2, 4, 6, 4, 5, 5, 2, 3, 3, 4, 4, 2, 3, 2, 3, 4, 3, 4, 4, 4, 4, 4, 4, 4, 4, 2, 3, 4, 4, 4, 4, 5, 5, 5, 5, 5, 5, 5, 5, 5, 2, 4, 5, 4, 6, 5, 5, 5, 5, 5, 5, 4, 5, 4, 6, 4, 4, 4, 3, 3, 3, 4, 4, 4, 4, 4, 4, 4, 4, 3, 4, 4, 2, 3, 4, 2, 6, 4, 4, 4, 4, 3, 4, 3, 4, 4, 6, 4, 4, 4, 4, 2, 4, 5]
NORMAL_ADVANCES = [5, 3, 4, 6, 6, 8, 7, 2, 3, 3, 4, 6, 3, 4, 4, 5, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 2, 3, 4, 6, 4, 6, 8, 6, 6, 6, 6, 6, 6, 6, 6, 4, 5, 6, 5, 8, 6, 6, 6, 6, 6, 6, 6, 6, 6, 8, 6, 6, 6, 3, 5, 3, 6, 5, 6, 5, 5, 5, 5, 5, 5, 5, 5, 4, 4, 5, 4, 6, 5, 5, 5, 5, 4, 5, 5, 5, 6, 6, 6, 5, 5, 4, 3, 4, 6]


def seconds(value):
    try:
        result = float(str(value).replace(',', '.'))
        return max(0, result) if math.isfinite(result) else 0
    except ValueError:
        return 0


def scroll_ms(text, width, small=False):
    advances = SMALL_ADVANCES if small else NORMAL_ADVANCES
    length = sum(advances[ord(c) - 32 if 32 <= ord(c) <= 126 else 31] for c in text)
    return 600 + max(0, length - width) / 6 * 1000 + 1200


def rectangle(identity, x, y, width, height, color):
    return dict(id=identity, type='rectangle', display='front', x=x, y=y,
                width=width, height=height, radius=0, fill='solid',
                fill_colors=[color], border_width=0)


def text(identity, value, x, y, width, font='normal', color='#FFFFFFFF'):
    return dict(id=identity, type='text', display='front', text=value or ' ',
                x=x, y=y, width=width, font=font, color=color,
                scroll_rate=360, scroll_start_delay=600, scroll_repeat_delay=1500)


ICONS = {
    'bolt': ['00001110','00011100','00111000','01111110','00011100','00111000','01110000','01100000','11000000','10000000'],
    'apple': ['00001100','00011000','00000000','01101100','11111110','11111000','11111000','11111110','01111100','00101000'],
    'blank': ['00000000'] * 10,
}


def icon_png(name):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    frame = int(name[-1]) if name.startswith('bolt-') else None
    rows = ICONS['bolt' if frame is not None else name]
    colors = [(255,176,24),(255,210,56),(255,244,150),(255,255,240),(255,218,72),(255,187,28)]
    sparks = [(7,8),(1,1),(6,6),(0,4),(7,2),(3,9)]
    raw = bytearray()
    for y, row in enumerate(rows):
        raw.append(0)
        for x, pixel in enumerate(row):
            rgba = (0, 0, 0, 0)
            if pixel == '1':
                color = colors[frame] if frame is not None else ((255,192,40) if name == 'bolt' else (255,255,255))
                if frame is not None and (x + y + frame) % 5 == 0:
                    color = (255,255,220)
                rgba = (*color,255)
            elif frame is not None and (x, y) == sparks[frame]:
                rgba = (255,230,130,255)
            raw.extend(rgba)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!IIBBBBB',8,10,8,6,0,0,0)) + chunk(b'IDAT',zlib.compress(raw)) + chunk(b'IEND',b'')


# Minimal bounded protobuf reader: State.updates.input.{button_event,switch_event}.
def fields(data):
    offset = 0
    def varint():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(data):
                raise ValueError('truncated protobuf')
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError('oversized varint')
    result = []
    while offset < len(data):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value = varint()
        elif wire == 2:
            size = varint()
            if size > len(data) - offset:
                raise ValueError('truncated protobuf field')
            value = data[offset:offset + size]
            offset += size
        elif wire in (1, 5):
            offset += 8 if wire == 1 else 4
            if offset > len(data):
                raise ValueError('truncated fixed field')
            continue
        else:
            raise ValueError('unsupported protobuf wire type')
        result.append((number, wire, value))
    return result


def inputs(data):
    def nested(blob, number):
        return [value for n, wire, value in fields(blob) if n == number and wire == 2]
    events = []
    for update in nested(data, 2):
        for event in nested(update, 11):
            for button in nested(event, 1):
                values = {n: v for n, wire, v in fields(button) if wire == 0}
                if values.get(2, 0) == 0 and values.get(1, 0) in (0, 1, 2):
                    events.append(('button', values.get(1, 0)))
            for switch in nested(event, 2):
                values = {n: v for n, wire, v in fields(switch) if wire == 0}
                events.append(('switch', values.get(1, 0)))
    return events


class StateSocket:
    """Small RFC 6455 client; handles masked sends, ping/pong and fragmentation."""
    def __init__(self, url):
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        self.sock = socket.create_connection((parsed.hostname, port), timeout=3)
        try:
            if parsed.scheme == 'https':
                self.sock = ssl.create_default_context().wrap_socket(self.sock, server_hostname=parsed.hostname)
            self.sock.settimeout(3)
            key = base64.b64encode(os.urandom(16)).decode()
            target = parsed.path + ('?' + parsed.query if parsed.query else '')
            request = (f'GET {target} HTTP/1.1\r\nHost: {parsed.netloc}\r\nUpgrade: websocket\r\n'
                       f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n')
            self.sock.sendall(request.encode())
            header = bytearray()
            while not header.endswith(b'\r\n\r\n'):
                header.extend(self.exact(1))
                if len(header) > 16384:
                    raise ValueError('oversized WebSocket handshake')
            lines = header.decode('latin1').split('\r\n')
            if len(lines[0].split()) < 2 or lines[0].split()[1] != '101':
                raise ValueError('WebSocket rejected: ' + lines[0])
            values = {name.lower(): value for name, value in (line.split(':', 1) for line in lines[1:] if ':' in line)}
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            if values.get('sec-websocket-accept', '').strip() != expected:
                raise ValueError('invalid WebSocket handshake')
            self.send(1, b'{"enable":true}')
        except Exception:
            self.close()
            raise

    def exact(self, count):
        result = bytearray()
        while len(result) < count:
            part = self.sock.recv(count - len(result))
            if not part:
                raise ConnectionError('WebSocket closed')
            result.extend(part)
        return bytes(result)

    def send(self, opcode, payload):
        mask = os.urandom(4)
        size = len(payload)
        header = bytes([128 | opcode, 128 | size]) if size < 126 else bytes([128 | opcode, 128 | 126]) + struct.pack('!H', size)
        self.sock.sendall(header + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)))

    def messages(self, stopped):
        fragments = bytearray()
        kind = None
        while not stopped.is_set():
            # Idle connections are normal. A timeout midway through a frame reconnects.
            self.sock.settimeout(1)
            try:
                first = self.exact(1)[0]
            except socket.timeout:
                continue
            self.sock.settimeout(3)
            second = self.exact(1)[0]
            size = second & 127
            if size == 126:
                size = struct.unpack('!H', self.exact(2))[0]
            elif size == 127:
                size = struct.unpack('!Q', self.exact(8))[0]
            if size > 1024 * 1024:
                raise ValueError('oversized WebSocket frame')
            mask = self.exact(4) if second & 128 else None
            payload = self.exact(size)
            if mask:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            opcode = first & 15
            if opcode == 8:
                return
            if opcode == 9:
                self.send(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode in (1, 2):
                kind, fragments = opcode, bytearray()
            elif opcode != 0 or kind is None:
                raise ValueError('invalid WebSocket continuation')
            fragments.extend(payload)
            if len(fragments) > 1024 * 1024:
                raise ValueError('oversized WebSocket message')
            if first & 128:
                if kind == 2:
                    yield bytes(fragments)
                kind, fragments = None, bytearray()

    def close(self):
        self.sock.close()


class Widget:
    def __init__(self, args):
        self.args = args
        host = args.host if '://' in args.host else 'http://' + args.host
        self.base = host.rstrip('/')
        self.token = args.token or os.environ.get('BUSY_HTTP_PASSWORD', '')
        self.stop = threading.Event()
        self.events = queue.Queue(maxsize=16)
        self.owned = 0
        self.apps_mode = True
        self.manager = False
        self.track = ('closed', 'Open Music', '', 0, 0)
        self.sampled = self.started = time.monotonic()
        self.cycle_start = self.started
        self.changed = 0
        self.feedback = ''
        self.feedback_until = 0
        self.levels = [0] * 8
        self.peaks = [0] * 8
        self.last_label = self.last_counter = self.last_icon = None
        self.assets_ready = False
        self.input_thread = None
        self.poll_thread = None
        self.results = queue.Queue(maxsize=1)
        self.commands = queue.Queue(maxsize=16)

    def request(self, path, data=None, method=None, binary=False):
        headers = {'X-API-Token': self.token} if self.token else {}
        if data is not None:
            headers['Content-Type'] = 'application/octet-stream' if binary else 'application/json'
            if not binary:
                data = json.dumps(data).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            detail = error.read(1000).decode('utf-8', errors='replace').strip()
            error.reason = f'{error.reason} [{request.method} {urllib.parse.urlsplit(path).path}]' + (f': {detail}' if detail else '')
            raise

    def clear(self):
        try:
            self.request('/api/display/draw?application_name=' + APP, method='DELETE')
        except urllib.error.HTTPError as error:
            # Some firmware returns 400 when clearing an app with no active display.
            # Cleanup must not prevent a valid subsequent upload/draw at startup.
            if error.code != 400:
                raise
            error.close()

    def state_loop(self):
        while not self.stop.is_set():
            stream = None
            try:
                query = '' if self.manager or not self.token else '?' + urllib.parse.urlencode({'x-api-token': self.token})
                stream = StateSocket(self.base + '/api/status/ws' + query)
                print('Hardware controls connected', flush=True)
                for data in stream.messages(self.stop):
                    for kind, value in inputs(data):
                        if kind == 'switch':
                            self.apps_mode = value == 3
                        elif self.apps_mode and time.monotonic() < self.owned:
                            try:
                                self.events.put_nowait(value)
                            except queue.Full:
                                pass
            except (OSError, ValueError, ConnectionError) as error:
                if not self.stop.is_set():
                    print('Button stream: ' + str(error), file=sys.stderr)
            finally:
                if stream:
                    stream.close()
                self.owned = 0
            self.stop.wait(3)

    def music_loop(self):
        while not self.stop.is_set():
            command = 'status'
            try:
                command = self.commands.get_nowait()
            except queue.Empty:
                pass
            try:
                if self.args.demo:
                    elapsed = time.monotonic() - self.started
                    track = ('playing', 'Karma Police', 'Radiohead', (198 + elapsed) % 264, 264)
                else:
                    completed = subprocess.run(['/usr/bin/osascript', '-e', SCRIPT, command], capture_output=True, text=True, timeout=10, check=True)
                    values = completed.stdout.rstrip('\n').split('\x1e')
                    if len(values) != 5:
                        raise ValueError('unexpected Music response')
                    track = (*values[:3], seconds(values[3]), seconds(values[4]))
                result = (track, command, time.monotonic())
                # Keep the latest sample, never an unbounded backlog.
                try:
                    self.results.get_nowait()
                except queue.Empty:
                    pass
                self.results.put_nowait(result)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print('Music: ' + str(error), file=sys.stderr)
            self.stop.wait(1)

    def apply_sample(self):
        try:
            track, command, sampled = self.results.get_nowait()
        except queue.Empty:
            return
        if track[1:3] != self.track[1:3]:
            self.cycle_start = self.changed = sampled
        self.track, self.sampled = track, sampled
        if command != 'status':
            self.feedback = ('PLAY' if track[0] == 'playing' else 'PAUSE') if command == 'toggle' else ('NEXT' if command == 'next' else 'PREV')
            self.feedback_until = time.monotonic() + 1.2

    def marquee(self, now):
        state, title, artist, _, _ = self.track
        song_ms = max(4000, scroll_ms(title, 46, True), scroll_ms(artist, 46, True))
        winamp_ms = scroll_ms('Powered by Winamp', 37)
        apple_ms = scroll_ms('and Apple Music', 37)
        llama = "It really whips the llama's ass!"
        base = song_ms + winamp_ms + apple_ms
        elapsed = (now - self.cycle_start) * 1000 % (base * 3 + scroll_ms(llama, 46))
        if now < self.feedback_until:
            return self.feedback, 'blank', False, 46
        if state not in ('playing', 'paused'):
            return title, 'blank', True, 46
        if elapsed >= base * 3:
            return llama, 'blank', False, 46
        phase = elapsed % base
        if phase < song_ms:
            return title, 'blank', True, 46
        if phase < song_ms + winamp_ms:
            return 'Powered by Winamp', 'bolt', False, 37
        return 'and Apple Music', 'apple', False, 37

    def draw(self, now):
        state, title, artist, position, duration = self.track
        playing = state == 'playing'
        value, icon, two_rows, width = self.marquee(now)
        label = (value, artist if two_rows else '', icon, two_rows)
        icon_asset = f'bolt-{int(now * 8) % 6}' if icon == 'bolt' and playing else icon
        elements = []
        if self.assets_ready and icon_asset != self.last_icon:
            elements.append(dict(id='credit-icon', type='image', display='front', path=f'credit-{icon_asset}.png', x=64, y=1))
        if label != self.last_label:
            elements.append(text('track', value, 26, 0 if two_rows else 1, width, 'small' if two_rows else 'normal'))
            elements.append(text('artist', artist if two_rows else ' ', 26, 7, 46, 'small', '#C0C8D0FF'))
        mode = int((now - self.started) / 12) % 3
        ms = now * 1000
        burst = max(0, 1 - (now - self.changed) / .8)
        elements.append(rectangle('visualizer-bg', 0, 0, 24, 9, '#060C08FF'))
        for x in range(8):
            wave = math.sin(ms / (130 + x * 23) + x * 1.7) + math.sin(ms / 410 + x * .9) * .6
            beat = max(0, math.sin(ms / 120 + x * .43)) ** 3
            target = max(1, min(12, int(3 + wave * 3 + beat * 7 + .5))) if playing else 0
            self.levels[x] = target if target > self.levels[x] else max(target, self.levels[x] - 1)
            self.peaks[x] = max(self.levels[x], self.peaks[x] - .25) if playing else 0
            def point(t):
                wave = (math.sin(t / 150 + x * .82) * math.cos(t / 680 + x * .31)) if mode == 2 else (.72 * math.sin(t / 170 + x * .7) + .28 * math.sin(t / 75 - x * 1.2))
                return max(0, min(3, int(1.5 + wave * 1.5 + .5)))
            for row in range(4):
                color = '#060C08FF'
                if not playing:
                    color = '#25432BFF' if row == 0 else color
                elif burst > 0 and row < math.ceil(burst * 4):
                    color = '#FFFFFFFF' if row == 3 else '#D4FF68FF'
                elif mode == 0:
                    if row == min(3, int(self.peaks[x] / 3)):
                        color = '#C6FFB0FF'
                    elif row * 3 < self.levels[x]:
                        color = ['#52F552FF','#88FF48FF','#E9ED43FF','#FF5353FF'][row]
                elif row == point(ms):
                    color = '#B0FFFFFF' if mode == 2 else '#B8FF70FF'
                elif row == point(ms - 100):
                    color = '#39B5CBFF' if mode == 2 else '#47A64BFF'
                elif row == point(ms - 220):
                    color = '#164557FF' if mode == 2 else '#1C542FFF'
                elif mode == 1 and min(1, point(ms)) <= row <= max(1, point(ms)):
                    color = '#298C38FF'
                elements.append(rectangle(f'cell-{x}-{row}', x * 3, 7 - row * 2, 2, 1, color))
        elapsed = min(2, max(0, now - self.sampled)) if playing else 0
        current = min(duration, position + elapsed) if duration > 0 else position + elapsed
        total = int(current)
        counter = f'{total // 60:02d}:{total % 60:02d}'
        if counter != self.last_counter:
            elements.append(text('counter', counter, 0, 10, 24, 'tiny', '#65FF62FF'))
        filled = int(46 * max(0, min(1, current / duration))) if duration > 0 else 0
        elements += [rectangle('rail',26,14,46,2,'#182518FF'),
                     rectangle('progress',26,14,max(1,filled),2,('#65FF62FF' if playing else '#879E87FF') if filled else '#182518FF'),
                     rectangle('progress-head',26+max(0,filled-1),14,1,2,'#FFFFFFFF' if filled else '#182518FF')]
        try:
            self.request('/api/display/draw', dict(application_name=APP, priority=20, elements=elements))
            self.owned = time.monotonic() + .5
            self.last_label, self.last_counter, self.last_icon = label, counter, icon_asset
            return True
        except urllib.error.HTTPError as error:
            self.owned = 0
            self.last_label = self.last_counter = self.last_icon = None
            if error.code != 409:
                raise
            return False

    def run(self):
        if not self.args.demo:
            try:
                self.manager = isinstance(json.loads(self.request('/api/_manager/state')), dict)
            except (OSError, ValueError):
                pass
        self.clear()
        for name in ['blank','bolt','apple'] + [f'bolt-{i}' for i in range(6)]:
            self.request('/api/assets/upload?' + urllib.parse.urlencode(dict(application_name=APP, file=f'credit-{name}.png')), icon_png(name), binary=True)
        self.assets_ready = True
        self.poll_thread = threading.Thread(target=self.music_loop, daemon=True)
        self.poll_thread.start()
        if not self.args.demo:
            self.input_thread = threading.Thread(target=self.state_loop, daemon=True)
            self.input_thread.start()
        print(f'{APP} → {self.base}; OK play/pause · BACK previous · START next', flush=True)
        try:
            while not self.stop.is_set():
                start = time.monotonic()
                self.apply_sample()
                try:
                    owned = self.draw(start)
                    try:
                        button = self.events.get_nowait()
                    except queue.Empty:
                        button = None
                    if button is not None and owned and self.apps_mode and not self.args.demo:
                        if self.manager:
                            owner = json.loads(self.request('/api/_manager/state')).get('screenOwner') or {}
                            owned = owner.get('applicationName') == APP
                        if owned:
                            try:
                                self.commands.put_nowait(['toggle','previous','next'][button])
                            except queue.Full:
                                pass
                except (OSError, ValueError) as error:
                    self.owned = 0
                    print('Display: ' + str(error), file=sys.stderr)
                    self.stop.wait(1)
                self.stop.wait(max(0, .125 - (time.monotonic() - start)))
        finally:
            self.stop.set()
            for thread in (self.input_thread, self.poll_thread):
                if thread:
                    thread.join(timeout=3)
            try:
                self.clear()
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='10.0.4.20', help='BUSY Bar, emulator, or Manager host[:port]')
    parser.add_argument('--token', default='', help='Wi-Fi API token (or BUSY_HTTP_PASSWORD environment variable)')
    parser.add_argument('--demo', action='store_true', help='Animated demo with sample metadata; no Music or hardware controls')
    args = parser.parse_args()
    if sys.platform != 'darwin' and not args.demo:
        parser.error('Apple Music requires macOS. Use --demo to preview on other platforms.')
    widget = Widget(args)
    signal.signal(signal.SIGINT, lambda *_: widget.stop.set())
    signal.signal(signal.SIGTERM, lambda *_: widget.stop.set())
    try:
        widget.run()
    except (OSError, ValueError) as error:
        widget.stop.set()
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
