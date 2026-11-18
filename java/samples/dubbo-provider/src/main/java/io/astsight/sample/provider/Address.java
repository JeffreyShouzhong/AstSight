package io.astsight.sample.provider;

import jakarta.validation.constraints.Pattern;
import jakarta.validation.constraints.Size;

public class Address {

    @Size(max = 64)
    private String city;

    @Pattern(regexp = "^\\d{6}$")
    private String zip;

    public String getCity() {
        return city;
    }

    public void setCity(String city) {
        this.city = city;
    }

    public String getZip() {
        return zip;
    }

    public void setZip(String zip) {
        this.zip = zip;
    }
}
