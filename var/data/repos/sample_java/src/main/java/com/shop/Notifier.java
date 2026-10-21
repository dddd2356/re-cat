package com.shop;

import org.springframework.stereotype.Component;

@Component
public class Notifier {
    public void send(String userId, String itemId) {
        String body = render(userId, itemId);
        deliver(userId, body);
    }

    private String render(String userId, String itemId) {
        return "receipt " + userId + " " + itemId;
    }

    private void deliver(String userId, String body) {
    }
}
