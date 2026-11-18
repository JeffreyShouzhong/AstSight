package io.astsight.sample.triple;

import org.apache.dubbo.config.annotation.DubboService;

/** 由 proto 生成的 Triple base class；只实现 unary Reserve。 */
@DubboService(
        interfaceClass = InventoryService.class,
        group = "inventory",
        version = "1.0.0"
)
public class InventoryServiceImpl
        extends DubboInventoryServiceTriple.InventoryServiceImplBase {

    @Override
    public ReserveResponse reserve(ReserveRequest request) {
        return ReserveResponse.newBuilder()
                .setAvailable("SKU-1".equals(request.getSku()))
                .setReservationId("TRIPLE-" + request.getSku())
                .build();
    }
}
