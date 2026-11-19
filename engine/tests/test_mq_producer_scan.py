"""MQ 生产者调用点扫描：Java/Python topic 与常量解析。"""
from __future__ import annotations

from pathlib import Path

from astsight.profile.mq import scan_mq_producers


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_java_producers_are_found(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/java/com/acme/OrderService.java",
        """
package com.acme;

class OrderService {
    static final String TOPIC = "orders-topic";

    void send(String body) {
        producer.send(new ProducerRecord<>(TOPIC, body));
        kafkaTemplate.send("pay-topic", body);
        channel.basicPublish("orders-exchange", "order.created", null, body.getBytes());
        rabbitTemplate.convertAndSend("order.queue", body);
        producer.send(new Message("rocket-topic", body.getBytes()));
        rocketMQTemplate.syncSend("notify-topic:tag", body);
    }
}
""",
    )
    result = scan_mq_producers(tmp_path)
    found = {(item["client"], item["topic"]) for item in result.producers}
    assert ("kafka", "orders-topic") in found
    assert ("kafka", "pay-topic") in found
    assert ("rabbitmq", "orders-exchange/order.created") in found
    assert ("rabbitmq", "/order.queue") in found
    assert ("rocketmq", "rocket-topic") in found
    assert ("rocketmq", "notify-topic") in found


def test_python_producers_resolve_constants(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "app/mq.py",
        """
TOPIC = "py-orders"
ROUTING = "order.created"

def publish(body):
    producer.send(TOPIC, body)
    channel.basic_publish(
        exchange="orders-exchange",
        routing_key=ROUTING,
        body=body,
    )
""",
    )
    result = scan_mq_producers(tmp_path)
    found = {(item["client"], item["topic"]) for item in result.producers}
    assert ("kafka", "py-orders") in found
    assert ("rabbitmq", "orders-exchange/order.created") in found


def test_dynamic_topic_is_reported_not_guessed(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/java/com/acme/Dynamic.java",
        """
class Dynamic {
    void send(String topic, String body) {
        producer.send(new ProducerRecord<>(topic, body));
    }
}
""",
    )
    result = scan_mq_producers(tmp_path)
    assert result.producers == []
    assert any("mq_topic_unresolved" in item for item in result.warnings)


def test_profiles_include_scanned_mq_producers(tmp_path: Path) -> None:
    from astsight.profile.builder import build_profiles

    _write(
        tmp_path,
        "src/main/java/com/acme/OrderController.java",
        """
class OrderController {
    void create() {
        kafkaTemplate.send("orders-topic", "{}");
    }
}
""",
    )
    profiles, _ = build_profiles(
        [
            {
                "method": "POST",
                "path": "/orders",
                "handler": "OrderController.create",
                "file": "src/main/java/com/acme/OrderController.java",
                "line": 3,
                "source": "framework_plugin",
            }
        ],
        [],
        project_root=tmp_path,
    )
    producers = profiles[0]["mq_producers"]
    assert producers[0]["client"] == "kafka"
    assert producers[0]["topic"] == "orders-topic"
