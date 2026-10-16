#!/usr/bin/env python3
"""Заглушка Telegram Bot API: принимает запросы бота, отдаёт заранее заготовленные обновления.

Состояние в /tmp/tgstate:
  updates.json  — очередь обновлений (можно дописывать на лету)
  sent.jsonl    — всё, что бот «отправил»
  file.bin      — содержимое, которое скармливается как скачанный документ
"""
import json, os, pathlib, sys, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE = pathlib.Path(os.environ.get("TG_STATE", "/tmp/tgstate"))
STATE.mkdir(parents=True, exist_ok=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "/file/" in self.path:
            data = (STATE / "file.bin").read_bytes() if (STATE / "file.bin").exists() else b"fake"
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self._json({"ok": True, "result": {}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = urllib.parse.parse_qs(self.rfile.read(length).decode())
        flat = {key: value[0] for key, value in payload.items()}
        method = self.path.rsplit("/", 1)[-1]

        if method == "getMe":
            return self._json({"ok": True, "result": {"username": "test_book_bot", "id": 42}})
        if method == "getUpdates":
            pending = json.loads((STATE / "updates.json").read_text()) if (STATE / "updates.json").exists() else []
            offset = int(flat.get("offset", 0))
            keep, answer = [], []
            for update in pending:
                if int(update["update_id"]) >= offset:
                    answer.append(update)      # выдаём и оставляем в очереди (проще для теста)
                keep.append(update)
            (STATE / "updates.json").write_text(json.dumps(keep))
            return self._json({"ok": True, "result": answer})
        if method == "sendMessage":
            with (STATE / "sent.jsonl").open("a") as handle:
                handle.write(json.dumps({"chat_id": flat.get("chat_id"), "text": flat.get("text")},
                                        ensure_ascii=False) + "\n")
            return self._json({"ok": True, "result": {"message_id": 1}})
        if method == "getFile":
            return self._json({"ok": True, "result": {"file_path": "books/x.epub", "file_size": 2048}})
        return self._json({"ok": True, "result": {}})


if __name__ == "__main__":
    port = int(sys.argv[1] if len(sys.argv) > 1 else 8123)
    print(f"заглушка Telegram на http://127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
