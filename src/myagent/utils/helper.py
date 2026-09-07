"""通用工具函数。"""

import uuid
from pathlib import Path


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
