package io.astsight.sidecar.dubbo;

public final class EchoRequest {

    private String name;

    public EchoRequest() {
    }

    public EchoRequest(String name) {
        this.name = name;
    }

    public String getName() {
        return name;
    }

    public void setName(String name) {
        this.name = name;
    }
}
