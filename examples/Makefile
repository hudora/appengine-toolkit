APPID?= e~wwwhudorade
OPENAPPID?= wwwhudorade
REPOSNAME=wwwhudorade
PRODUCTIONURL?=https://example.com/
DEVPAGE?=/selftest.html

MYPYTHONPATH=lib/site-packages

-include ./lib/appengine-toolkit/include.mk

boot:
	git submodule update --init
	pip install --target lib/site-packages -r requirements.txt
