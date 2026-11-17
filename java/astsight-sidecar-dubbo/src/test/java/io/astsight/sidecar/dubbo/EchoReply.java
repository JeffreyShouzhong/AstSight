package io.astsight.sidecar.dubbo;

public final class EchoReply {

    private String message;
    private String tenant;

    public EchoReply() {
    }

    public EchoReply(String message, String tenant) {
        this.message = message;
        this.tenant = tenant;
    }

    public String getMessage() {
        return message;
    }

    public void setMessage(String message) {
        this.message = message;
    }

    public String getTenant() {
        return tenant;
    }

    public void setTenant(String tenant) {
        this.tenant = tenant;
    }
}
