# Root entrypoint required by Vercel Python detection
# The actual logic lives in api/cron.py (triggered by Vercel cron)
from http.server import BaseHTTPRequestHandler
import json

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "Doja Delivery Bot running ✅", "cron": "/api/cron"}).encode())

    def log_message(self, format, *args):
        pass
