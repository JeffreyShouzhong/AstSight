package io.astsight.agent.core;

import com.fasterxml.jackson.databind.ObjectMapper;

final class Json {

    static final ObjectMapper MAPPER = new ObjectMapper();

    private Json() {
    }
}
