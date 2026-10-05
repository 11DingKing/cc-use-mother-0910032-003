"""启动志愿权益兑换后端服务。

用法：python3 tools/serve.py [--host 127.0.0.1] [--port 8080]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from redemption.api import main

if __name__ == "__main__":
    main()
