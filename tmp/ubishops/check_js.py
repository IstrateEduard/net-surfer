import os, sys, json, pathlib, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from browser import engine, js, network
from browser.html_parser import parse
page=engine.Page(network.URL('https://www.ubishops.ca/'))
page.source=pathlib.Path(__file__).with_name('index.html').read_text(encoding='utf-8')
page.document=parse(page.source)
engine._run_scripts(page, 1366, lambda *a: print(*a,flush=True))
print('\n'.join(page.log),flush=True)
if page.js:
 for i in range(30):
  page.js.tick(); time.sleep(.1)
 print('FINAL LOG\n'+'\n'.join(page.log),flush=True)
 page.js.close()
