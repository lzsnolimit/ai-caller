"""Independent UI host. API address is selected in the browser, no AI keys here."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __name__ == "__main__":
    root = str(Path(__file__).resolve().parent)
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    print("Client: http://127.0.0.1:5173", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 5173), partial(QuietHandler, directory=root)).serve_forever()
