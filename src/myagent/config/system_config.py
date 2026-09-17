"""系统级配置。

LLM 连接参数（模型 / 地址 / Key）统一从项目根 .env 读取，供所有 agent 共用：

- MODEL      : 模型名
- BASE_URL   : 兼容接口地址
- ARK_API_KEY: API Key

只在这里解析一次环境变量；各 agent 通过下面的取值函数获取，不再各自硬编码模型名。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

local_data_path = Path("./localData")

# 只填充进程启动时尚未设置的变量，重复调用无副作用；
# 放在模块导入时执行，保证取值函数读到的是 .env 的值。
load_dotenv()


def get_llm_model() -> str:
    """返回 .env 中配置的模型名（MODEL）。"""
    model = os.getenv("MODEL")
    if not model:
        raise RuntimeError("缺少环境变量 MODEL，请在项目根 .env 中配置")
    return model


def get_llm_base_url() -> str:
    """返回 .env 中配置的接口地址（BASE_URL）。"""
    base_url = os.getenv("BASE_URL")
    if not base_url:
        raise RuntimeError("缺少环境变量 BASE_URL，请在项目根 .env 中配置")
    return base_url


def get_llm_api_key() -> str:
    """返回 .env 中配置的 API Key（ARK_API_KEY）。"""
    return os.getenv("ARK_API_KEY", "")
