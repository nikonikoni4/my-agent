"""evalue 测试的共享 fixture（可 import 的构造件在 helpers.py / fakes.py）。"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def short_tmp():
    """短路径临时目录。

    session 文件路径会把「数据根全路径」编码成一层目录名，Windows 上 pytest 的 tmp_path
    太长会撞到 MAX_PATH，故涉及 session 落盘的用例用短目录。
    """
    path = Path(tempfile.gettempdir()) / f"ev-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
