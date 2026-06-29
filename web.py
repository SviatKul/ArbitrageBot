#!/usr/bin/env python3
"""
Веб-интерфейс для управления арбитражным ботом.

Запуск:
    python web.py

Открыть в браузере:
    http://localhost:8080
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from web.app import create_app

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    host = "0.0.0.0" if os.environ.get("RAILWAY_ENVIRONMENT") else "127.0.0.1"
    app = create_app()
    print(f"\n  ⚡ Arbitrage Bot Dashboard → http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False, threaded=True)
