import sys, pathlib, json, tkinter
sys.path.insert(0,str(pathlib.Path(__file__).parents[2]))
from browser import engine,layout
root=tkinter.Tk(); root.withdraw()
page=engine.Page(engine.network.URL('https://www.ubishops.ca/'))
engine._finish(page,pathlib.Path(__file__).with_name('index.html').read_text(encoding='utf-8'),1440)
print('FONT DESCRIPTORS',len(page.font_faces),'LOADED',list(page.web_fonts.faces) if hasattr(page,'web_fonts') else None)
print('\n'.join(page.log))
ctx=layout.LayoutContext(page.base_url,page.images,1440,900)
doc=layout.DocumentLayout(page.document,ctx); doc.layout(1440,900)
records=[]
stack=[(doc.root,0)]
while stack:
 b,d=stack.pop(); n=b.node
 kids=list(b.children)+list(b.abs_boxes)
 for line in getattr(b,'lines',[]):
  kids += [f.box for f in line.frags if isinstance(f,layout.AtomFrag)]
 if n is not None:
  rec={'tag':n.tag,'class':n.attributes.get('class'),'id':n.attributes.get('id'),'depth':d,'box':[b.x,b.y,b.width,b.height],'style':{k:n.style.get(k) for k in ['display','font-family','width','height','min-height','max-width','margin-left','margin-right','position','background-size']}}
  records.append(rec)
 stack.extend((c,d+1) for c in reversed(kids))
pathlib.Path('screens/ubishops-boxes.json').write_text(json.dumps(records,indent=2),encoding='utf-8')
if page.js: page.js.close()
root.destroy()
