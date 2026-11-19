package io.astsight.sample.mq;

import com.rabbitmq.client.AMQP;
import com.rabbitmq.client.impl.ChannelN;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.rocketmq.client.producer.DefaultMQProducer;
import org.apache.rocketmq.common.message.Message;

import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Collections;
import java.util.Properties;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/**
 * MQ 生产者拦截联调样例：只用真实客户端构造消息，不连接 broker。
 *
 * <p>RabbitMQ 找不到可用连接时，用 Unsafe 分配一个未初始化的 ChannelN；
 * Agent 在 basicPublish 入口拦截后不会访问任何字段。</p>
 */
public final class MqProducerSample {

    public static void main(String[] args) throws Exception {
        String client = args[0];
        String topic = args[1];
        String body = args[2];
        if ("kafka".equals(client)) {
            kafka(topic, body);
        } else if ("rabbitmq".equals(client)) {
            rabbit(topic, body);
        } else if ("rocketmq".equals(client)) {
            rocket(topic, body);
        } else {
            throw new IllegalArgumentException("unknown client: " + client);
        }
        System.out.println("astsight-mq-sample:ok:" + client);
        // Kafka producer 有后台线程，直接退出避免测试进程挂起。
        System.exit(0);
    }

    private static void kafka(String topic, String body) throws Exception {
        Properties props = new Properties();
        props.put("bootstrap.servers", "127.0.0.1:1");
        props.put("key.serializer",
                "org.apache.kafka.common.serialization.StringSerializer");
        props.put("value.serializer",
                "org.apache.kafka.common.serialization.StringSerializer");
        props.put("max.block.ms", "1000");
        props.put("request.timeout.ms", "1000");
        props.put("delivery.timeout.ms", "1000");
        KafkaProducer<String, String> producer = new KafkaProducer<>(props);
        Future<?> future = producer.send(
                new ProducerRecord<>(topic, "K-1", body),
                (metadata, error) -> {
                    if (error != null) {
                        System.err.println("kafka callback error: " + error);
                    }
                });
        future.get(5, TimeUnit.SECONDS);
        producer.close(Duration.ofSeconds(1));
    }

    private static void rabbit(String topic, String body) throws Exception {
        // topic = exchange/routingKey
        int split = topic.indexOf('/');
        String exchange = split < 0 ? "" : topic.substring(0, split);
        String routingKey = split < 0 ? topic : topic.substring(split + 1);
        ChannelN channel = allocate(ChannelN.class);
        AMQP.BasicProperties props = new AMQP.BasicProperties.Builder()
                .headers(Collections.singletonMap("trace", "t1"))
                .build();
        channel.basicPublish(
                exchange, routingKey, props, body.getBytes(StandardCharsets.UTF_8));
    }

    private static void rocket(String topic, String body) throws Exception {
        DefaultMQProducer producer = new DefaultMQProducer("astsight-sample");
        producer.setNamesrvAddr("127.0.0.1:9876");
        Message message = new Message(
                topic, body.getBytes(StandardCharsets.UTF_8));
        message.setKeys("K-1");
        message.putUserProperty("trace", "t1");
        Object result = producer.send(message);
        System.out.println("rocketmq send result: " + result);
    }

    @SuppressWarnings("unchecked")
    private static <T> T allocate(Class<T> type) throws Exception {
        Field field = Class.forName("sun.misc.Unsafe").getDeclaredField("theUnsafe");
        field.setAccessible(true);
        Object unsafe = field.get(null);
        return (T) unsafe.getClass()
                .getMethod("allocateInstance", Class.class)
                .invoke(unsafe, type);
    }

    private MqProducerSample() {
    }
}
