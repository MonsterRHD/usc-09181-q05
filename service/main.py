"""服务入口：海外保单风险处置台。

- DESK_STORE_PATH：事件存储文件（JSONL）。缺省为纯内存，重启即清空；
  指定后系统恢复时自动重放，处置链仍按事故发生顺序整理。
- PORT：监听端口，缺省 8000。

启动：python -m service.main
"""

import os

from service.desk import Desk, EventStore
from service.desk.api import create_server


def build_desk() -> Desk:
    return Desk(EventStore(os.getenv("DESK_STORE_PATH") or None))


def run():
    create_server(build_desk(), int(os.getenv("PORT", "8000"))).serve_forever()


if __name__ == "__main__":
    run()
