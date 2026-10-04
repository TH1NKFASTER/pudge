from __future__ import annotations
import json
import os
import sys
from urllib.request import Request, urlopen
from urllib.parse import urlparse

def main() -> int:
    target = os.environ.get('PUDGE_ASSISTANT_URL', '')
    url = urlparse(target)
    if url.scheme != 'http' or url.hostname != '127.0.0.1' or url.path != '/api/mpv/explain':
        return 1
    try:
        payload = sys.argv[1].encode('utf-8')
        request = Request(target, data=payload, method='POST', headers={'Content-Type':'application/json',
                          'X-Pudge-Token':os.environ.get('PUDGE_ASSISTANT_TOKEN','')})
        with urlopen(request, timeout=8) as response:
            result = json.load(response)
        return 0 if result.get('ok') else 1
    except (OSError, ValueError, IndexError):
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
