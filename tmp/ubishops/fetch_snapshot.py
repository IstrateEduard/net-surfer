import pathlib, urllib.request, json, concurrent.futures
from html.parser import HTMLParser
from urllib.parse import urljoin
out = pathlib.Path(__file__).parent
url = 'https://www.ubishops.ca/'
html = urllib.request.urlopen(url, timeout=30).read()
(out/'index.html').write_bytes(html)
class Parser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'link' and a.get('rel') == 'stylesheet' and a.get('href'):
            self.links.append(urljoin(url, a['href']))
p = Parser()
p.feed(html.decode('utf-8'))
def download(pair):
    i, u = pair
    try:
        data = urllib.request.urlopen(u, timeout=30).read()
        name = f'style-{i:02d}.css'
        (out/name).write_bytes(data)
        return dict(url=u, file=name, bytes=len(data))
    except Exception as e:
        return dict(url=u, error=str(e))
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    manifest = list(pool.map(download, enumerate(p.links)))
(out/'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
print(json.dumps(manifest, indent=2))
