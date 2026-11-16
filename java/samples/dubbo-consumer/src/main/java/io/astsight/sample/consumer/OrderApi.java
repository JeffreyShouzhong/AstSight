package io.astsight.sample.consumer;

/** 消费端接口：`dep_id` 里的 service 取的是这个全限定名。 */
public interface OrderApi {

    OrderView create(String id);
}
