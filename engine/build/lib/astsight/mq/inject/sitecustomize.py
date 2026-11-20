"""Python SUT 启动钩子：patch kafka-python / pika 生产者。

由引擎把本目录加入 `PYTHONPATH` 并设置 `ASTSIGHT_MQ_CAPTURE=1` 后自动加载。
只做生产者边界拦截，不连接 broker。
"""
from __future__ import annotations

try:
    import capture

    capture.install()
except Exception:
    # SUT 不能因为采集补丁起不来；真正的 MQ 断言会因消息缺失而失败。
    pass
