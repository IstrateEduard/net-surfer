import subprocess,sys
names=['tests.test_browser.BrowserWindowTests.'+n for n in ['test_address_bar_and_error_page','test_click_link_back_forward_reload','test_fragment_link_scrolls','test_resize_relayouts','test_scrolling_keys','test_type_and_submit_form']]+['tests.test_js.JSWindowTests.test_click_handler_and_form']
failed=[]
for name in names:
 result=subprocess.run([sys.executable,'-m','unittest',name],capture_output=True,text=True,timeout=60)
 print(name+': '+('PASS' if result.returncode==0 else 'FAIL'),flush=True)
 if result.returncode:
  print(result.stdout+result.stderr,flush=True); failed.append(name)
print('Passed %d/%d window tests'%(len(names)-len(failed),len(names)),flush=True)
sys.exit(bool(failed))
