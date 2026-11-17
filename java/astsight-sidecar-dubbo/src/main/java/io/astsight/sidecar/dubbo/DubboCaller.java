package io.astsight.sidecar.dubbo;

/** 便于协议层单测替换真实调用。 */
interface DubboCaller {

    InvokeOutcome invoke(InvokeRequest request);
}
