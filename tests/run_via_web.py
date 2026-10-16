#!/usr/bin/env python3
"""Запускает книгу из папки через страницу: POST /run/<имя>. 303 — это нормальный ответ."""
import os, sys, urllib.error, urllib.parse, urllib.request

name = sys.argv[1]
url = os.environ["WEB_BASE"] + "/run/" + urllib.parse.quote(name)
request = urllib.request.Request(url, data=urllib.parse.urlencode({}).encode())
try:
    urllib.request.urlopen(request, timeout=120).read()
except urllib.error.HTTPError as exc:            # 303 See Other приходит как исключение
    if exc.code != 303:
        raise
print(f"  книга {name} поставлена в очередь со страницы")
