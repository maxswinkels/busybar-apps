import argparse
import base64
import hashlib
import socket
import struct
import threading
import time
import unittest
import urllib.error
from app import Widget, StateSocket, inputs, seconds, scroll_ms, icon_png


class WidgetTests(unittest.TestCase):
    def widget(self):
        return Widget(argparse.Namespace(host='127.0.0.1:8080', token='', demo=True))

    def test_localized_timestamps_and_proto_defaults(self):
        self.assertAlmostEqual(seconds('198,334'), 198.334)
        self.assertEqual(seconds('missing value'), 0)
        self.assertEqual(inputs(bytes([18,4,90,2,10,0])), [('button',0)])
        self.assertEqual(inputs(bytes([18,6,90,4,10,2,8,2])), [('button',2)])
        self.assertEqual(inputs(bytes([18,6,90,4,10,2,16,1])), [])
        self.assertEqual(inputs(bytes([18,6,90,4,18,2,8,3])), [('switch',3)])
        with self.assertRaises(ValueError):
            inputs(bytes([18,99]))

    def test_frames_fit_display_and_ids_do_not_accumulate(self):
        w = self.widget()
        w.track = ('playing','Song','Artist',100,200)
        w.assets_ready = True
        seen, frames = set(), []
        w.request = lambda path, data=None, **kw: frames.append(data) if data else b'{}'
        for offset in range(80):
            w.draw(w.started + offset)
        for frame in frames:
            elements = frame['elements']
            self.assertEqual(len({e['id'] for e in elements}), len(elements))
            for e in elements:
                seen.add(e['id'])
                self.assertGreaterEqual(e['x'], 0)
                self.assertGreaterEqual(e['y'], 0)
                self.assertLessEqual(e['x'] + e.get('width',8),72)
                self.assertLessEqual(e['y'] + e.get('height',0),16)
        self.assertLess(len(seen),64)
        self.assertTrue(any(e['id']=='counter' for f in frames for e in f['elements']))

    def test_credit_timing_waits_for_longest_row(self):
        w = self.widget()
        artist = 'An artist with a very long name that needs scrolling'
        w.track = ('playing','Song',artist,0,200)
        duration = max(4000,scroll_ms('Song',46,True),scroll_ms(artist,46,True))
        self.assertTrue(w.marquee(w.cycle_start+(duration-1)/1000)[2])
        self.assertEqual(w.marquee(w.cycle_start+(duration+1)/1000)[1], 'bolt')

    def test_yields_on_409_and_restores_all_text(self):
        w = self.widget()
        def reject(*a, **kw):
            raise urllib.error.HTTPError('url',409,'busy',{},None)
        w.request = reject
        self.assertFalse(w.draw(time.monotonic()))
        self.assertEqual(w.owned,0)
        self.assertIsNone(w.last_label)

    def test_cleanup_400_does_not_block_startup_but_auth_errors_propagate(self):
        w = self.widget()
        def reject400(*args, **kwargs):
            raise urllib.error.HTTPError('url',400,'Bad Request',{},None)
        w.request = reject400
        w.clear()
        def reject401(*args, **kwargs):
            raise urllib.error.HTTPError('url',401,'Unauthorized',{},None)
        w.request = reject401
        with self.assertRaises(urllib.error.HTTPError) as caught:
            w.clear()
        self.assertEqual(caught.exception.code,401)
        caught.exception.close()

    def test_icon_frames_are_real_distinct_pngs(self):
        frames = [icon_png(f'bolt-{i}') for i in range(6)]
        self.assertEqual(len(set(frames)),6)
        for image in frames:
            self.assertEqual(image[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('!II',image[16:24]),(8,10))

    def test_websocket_handshake_preserves_case_and_sends_masked_subscription(self):
        listener=socket.socket()
        listener.bind(('127.0.0.1',0))
        listener.listen(1)
        received=[]
        def server():
            client,_=listener.accept()
            with client:
                request=b''
                while not request.endswith(b'\r\n\r\n'):
                    request+=client.recv(1)
                key=next(line.split(b': ',1)[1] for line in request.split(b'\r\n') if line.startswith(b'Sec-WebSocket-Key:'))
                accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                client.sendall(b'HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n')
                header=client.recv(2)
                size=header[1]&127
                mask=b''
                while len(mask)<4:mask+=client.recv(4-len(mask))
                payload=b''
                while len(payload)<size:payload+=client.recv(size-len(payload))
                received.append(bytes(b^mask[i%4] for i,b in enumerate(payload)))
                # Fragmented binary protobuf interleaved with a ping.
                client.sendall(b'\x02\x03abc\x89\x01!\x80\x03def')
                time.sleep(.1)
        thread=threading.Thread(target=server)
        thread.start()
        stream=StateSocket('http://127.0.0.1:'+str(listener.getsockname()[1])+'/api/status/ws')
        try:
            self.assertEqual(next(stream.messages(threading.Event())),b'abcdef')
            self.assertEqual(received,[b'{"enable":true}'])
        finally:
            stream.close();thread.join();listener.close()


if __name__=='__main__':unittest.main()
