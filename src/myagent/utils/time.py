import datetime

def now()->str:
    """返回当前本地时间的 isoformat 字符串。

    Returns:
        当前时间，格式同 datetime.datetime.now().isoformat()。
    """
    return datetime.datetime.now().isoformat()