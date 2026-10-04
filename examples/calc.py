"""示例模块：简单的四则运算，供 unitgen 演示用。"""


def add(a, b):
    """返回 a + b。"""
    return a + b


def divide(a, b):
    """返回 a / b；b 为 0 时抛 ValueError。"""
    if b == 0:
        raise ValueError("除数不能为 0")
    return a / b
