import sys, pathlib, unittest
sys.path.insert(0,str(pathlib.Path(__file__).parents[2]))
loader=unittest.TestLoader()
suite=loader.discover('tests')
def cases(suite):
 for item in suite:
  if isinstance(item,unittest.TestSuite): yield from cases(item)
  else: yield item
selected=unittest.TestSuite(t for t in cases(suite) if '.BrowserWindowTests.' not in t.id() and '.JSWindowTests.' not in t.id())
result=unittest.TextTestRunner(verbosity=1).run(selected)
sys.exit(not result.wasSuccessful())
