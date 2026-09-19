"""通用工具函数。"""

import uuid
from pathlib import Path
from typing import Any

import yaml


def project_path_to_session_folder(project_path: Path, session_folder: Path) -> Path:
    """把项目路径编码为会话根目录下的项目文件夹路径。

    规则：路径中的分隔符（/ \\ :）替换为 -，末尾追加 uuid5(路径) 的前
    8 位十六进制，保证不同项目路径不生成同一个文件夹。名字有损不可逆，
    项目真实路径以 session 文件第一行 meta 里的 cwd 为准。

    先 resolve 规范化，保证同一路径每次编码出的文件夹名一致。
    """
    normalized = str(Path(project_path).resolve())
    name = normalized.replace("\\", "-").replace("/", "-").replace(":", "-")
    suffix = uuid.uuid5(uuid.NAMESPACE_URL, normalized).hex[:8]
    return Path(session_folder) / f"{name}-{suffix}"


def read_yaml(path: Path | str) -> Any:
    """读取一个 YAML 文件，返回解析结果。

    编码固定 utf-8，不依赖平台默认编码——否则同一份配置在不同机器上可能
    读出不同内容；用 safe_load 而非 load，不实例化任意 Python 对象。

    空文件返回 None 而不是 {}：这是 safe_load 对空内容的既有行为，调用方
    按配置用时需自行判断，否则会以 TypeError 的形式炸在后续取值上，看不出
    "文件是空的"这个真因。
    """
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))
