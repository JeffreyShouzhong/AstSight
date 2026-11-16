package io.astsight.sample.dubbo;

/** 下游 Dubbo 服务接口；画像要求接口源码在同一仓库里。 */
public interface OrderApi {

    OrderView create(String id);
}
