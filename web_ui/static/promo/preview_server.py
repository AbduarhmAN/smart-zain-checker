"""Isolated campaign preview. Binds to localhost and exposes campaign assets only."""
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import re

ROOT = Path(__file__).resolve().parent

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,directory=str(ROOT),**kwargs)

    def log_message(self,*args):
        pass

    def do_POST(self):
        query=urlparse(self.path)
        if query.path!='/__studio/export':
            self.send_error(404); return
        origin=self.headers.get('Origin')
        if origin and origin!='http://127.0.0.1:5062':
            self.send_error(403); return
        name=parse_qs(query.query).get('name',[''])[0]
        if not re.fullmatch(r'hadiyat-check-[a-z0-9-]+\.(png|zip|mp4|webm)',name):
            self.send_error(400); return
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=64*1024*1024:
            self.send_error(413); return
        data=self.rfile.read(size)
        if len(data)!=size:
            self.send_error(400); return
        out=ROOT/'exports'/name
        out.parent.mkdir(exist_ok=True)
        out.write_bytes(data)
        self.send_response(200); self.send_header('Content-Length','0'); self.end_headers()

if __name__=='__main__':
    print('Campaign preview: http://127.0.0.1:5062/index.html',flush=True)
    ThreadingHTTPServer(('127.0.0.1',5062),Handler).serve_forever()
