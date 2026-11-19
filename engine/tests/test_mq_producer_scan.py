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


def test_java_value_annotation_resolves_properties(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/resources/application.properties",
        "mq.orders-topic=orders-topic\n",
    )
    _write(
        tmp_path,
        "src/main/java/com/acme/OrderService.java",
        """
class OrderService {
    @Value("${mq.orders-topic}")
    private String topic;

    void send(String body) {
        kafkaTemplate.send(topic, body);
    }
}
""",
    )
    result = scan_mq_producers(tmp_path)
    item = next(item for item in result.producers if item["client"] == "kafka")
    assert item["topic"] == "orders-topic"
    assert item["topic_source"] == "config"
    assert item["config_key"] == "mq.orders-topic"


def test_java_value_default_is_used_and_missing_warns(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/java/com/acme/Notification.java",
        """
class Notification {
    @Value("${mq.notify-topic:fallback-topic}")
    private String fallback;

    @Value("${mq.pay-topic}")
    private String missing;

    void send(String body) {
        kafkaTemplate.send(fallback, body);
        kafkaTemplate.send(missing, body);
    }
}
""",
    )
    result = scan_mq_producers(tmp_path)
    found = {item["topic"] for item in result.producers}
    assert "fallback-topic" in found
    assert all("pay-topic" not in topic for topic in found)
    assert any(
        "mq_topic_config_missing" in item and "mq.pay-topic" in item
        for item in result.warnings
    )


def test_spring_yml_nested_and_relaxed_binding(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/resources/application.yml",
        """
mq:
  orders:
    topic: orders-topic
  pay-topic: pay-topic
""",
    )
    _write(
        tmp_path,
        "src/main/java/com/acme/OrderService.java",
        """
class OrderService {
    @Value("${mq.orders.topic}")
    private String orders;

    void send(String body) {
        kafkaTemplate.send(orders, body);
    }
}
""",
    )
    _write(
        tmp_path,
        "src/main/java/com/acme/MqProperties.java",
        """
@ConfigurationProperties(prefix = "mq")
class MqProperties {
    private String payTopic;
}
""",
    )
    _write(
        tmp_path,
        "src/main/java/com/acme/PayService.java",
        """
class PayService {
    private final MqProperties properties;

    void send(String body) {
        kafkaTemplate.send(properties.payTopic, body);
    }
}
""",
    )
    result = scan_mq_producers(tmp_path)
    found = {item["topic"] for item in result.producers}
    assert "orders-topic" in found
    assert "pay-topic" in found


def test_conflicting_config_values_are_ambiguous(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/resources/application.properties",
        "mq.topic=orders-topic\n",
    )
    _write(
        tmp_path,
        "src/main/resources/application-prod.properties",
        "mq.topic=prod-topic\n",
    )
    _write(
        tmp_path,
        "src/main/java/com/acme/OrderService.java",
        """
class OrderService {
    @Value("${mq.topic}")
    private String topic;

    void send(String body) {
        kafkaTemplate.send(topic, body);
    }
}
""",
    )
    result = scan_mq_producers(tmp_path)
    assert result.producers == []
    assert any("mq_topic_config_ambiguous" in item for item in result.warnings)


def test_unresolved_identifiers_warn_for_all_java_clients(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/main/java/com/acme/Dynamic.java",
        """
class Dynamic {
    void send(String topic, String exchange, String routing, String body) {
        kafkaTemplate.send(topic, body);
        channel.basicPublish(exchange, routing, null, body.getBytes());
        rocketMQTemplate.syncSend(topic, body);
    }
}
""",
    )
    result = scan_mq_producers(tmp_path)
    assert result.producers == []
    assert sum("mq_topic_unresolved" in item for item in result.warnings) >= 3


def test_python_getenv_defaults_and_dotenv(tmp_path: Path) -> None:
    _write(tmp_path, ".env", "MQ_ORDERS_TOPIC=env-orders-topic\n")
    _write(
        tmp_path,
        "app/mq.py",
        """
import os

TOPIC = os.getenv("MQ_ORDERS_TOPIC")
FALLBACK = os.getenv("MQ_FALLBACK_TOPIC", "fallback-topic")
MISSING = os.getenv("MQ_MISSING_TOPIC")


def publish(body):
    producer.send(TOPIC, body)
    producer.send(FALLBACK, body)
    producer.send(MISSING, body)
""",
    )
    result = scan_mq_producers(tmp_path)
    found = {item["topic"] for item in result.producers}
    assert "env-orders-topic" in found
    assert "fallback-topic" in found
    assert any(
        "mq_topic_config_missing" in item and "MQ_MISSING_TOPIC" in item
        for item in result.warnings
    )


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
