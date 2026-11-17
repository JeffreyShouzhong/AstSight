package io.astsight.sidecar.dubbo;

import java.time.LocalDate;

public final class EchoRequest {

    private String name;
    private LocalDate birthday;

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

    public LocalDate getBirthday() {
        return birthday;
    }

    public void setBirthday(LocalDate birthday) {
        this.birthday = birthday;
    }
}
