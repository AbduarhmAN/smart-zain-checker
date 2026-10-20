"""Route requests transport to each test's existing urllib fixture, never a socket."""
import json
import urllib.error
import urllib.request


class Response:
    def __init__(self, body, status):
        self.body = body
        self.status_code = status

    def json(self): return json.loads(self.body)
    def iter_content(self, chunk_size=65536):
        for offset in range(0,len(self.body),chunk_size):
            yield self.body[offset:offset+chunk_size]
    def close(self): pass


def request(url, *, payload=None, timeout=25):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url,data=data,headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as response:
            return Response(response.read(),getattr(response,'status',200))
    except urllib.error.HTTPError as exc:
        return Response(exc.read(),exc.code)


def post(session,url,*,json=None,timeout=25,**kwargs):
    return request(url,payload=json,timeout=timeout)


def get(session,url,*,timeout=25,**kwargs):
    return request(url,timeout=timeout)
