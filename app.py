#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库根入口：等价于 `python3 web/server.py`。

    pip install -r requirements.txt
    python3 app.py            # http://127.0.0.1:8000
"""

import os

from web.server import app  # noqa: F401  (re-export for flask CLI / gunicorn)

if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        threaded=True,
    )
