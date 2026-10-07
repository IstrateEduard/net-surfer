import pathlib,json,sys
sys.path.insert(0,str(pathlib.Path(__file__).parents[2]))
from browser import css_parser,style
from browser.html_parser import parse
root=pathlib.Path(__file__).parent
manifest=json.loads((root/'manifest.json').read_text())
by_url={m['url']:(root/m['file']).read_text(encoding='utf-8') for m in manifest if 'file' in m}
doc=parse((root/'index.html').read_text(encoding='utf-8'))
rules=[]
for el in doc.descendants():
    if getattr(el,'tag',None)=='style': text=el.text_content()
    elif getattr(el,'tag',None)=='link' and el.attributes.get('href') in by_url: text=by_url[el.attributes['href']]
    else: continue
    got,_=css_parser.parse_stylesheet(text,viewport_width=1440,order_start=len(rules))
    rules.extend(got)
style.VIEWPORT['width']=1440
style.StyleEngine(rules).style_tree(doc)
print('rules',len(rules))
print('root vars',json.dumps({k:v for k,v in doc.style['-vars'].items() if k in ['--font-heading','--color1','--color10','--light-purple','--global--content-size','--global-vw','--wp--preset--color--theme-palette-1','--wp--preset--color--theme-palette-10','--global-content-width']},indent=2))
for el in doc.descendants():
    cls=getattr(el,'attributes',{}).get('class','')
    if 'card-grid-block__item' in cls or ('wp-block-kadence-rowlayout' in cls) or getattr(el,'tag',None)=='h1':
        print(el.tag,cls,json.dumps({k:el.style.get(k) for k in ['background-color','background-image','background-size','color','width','max-width','font-family','font-size']}))
