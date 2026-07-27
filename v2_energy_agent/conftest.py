"""pytest 根级配置：把 src 加入 sys.path，保证无安装态可直跑。"""
import sys
from pathlib import Path

_SRC = str(Path(__file__).parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)