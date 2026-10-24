#!/usr/bin/env python3
"""g2fan 启动器：确保包内模块可导入后进入守护进程。"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from g2fan.daemon import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
