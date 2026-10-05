"""启动 HTTP 服务：PYTHONPATH=src python3 -m redemption（PORT 环境变量可改端口）。"""
from __future__ import annotations

import os

from .api import serve

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    server = serve(port=port)
    print(f"志愿权益兑换服务已启动：http://{server.server_address[0]}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
