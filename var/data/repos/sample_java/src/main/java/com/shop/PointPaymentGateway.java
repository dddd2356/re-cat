package com.shop;

import org.springframework.stereotype.Component;

@Component
public class PointPaymentGateway implements PaymentGateway {
    public boolean pay(String userId, String itemId, int qty) {
        return deduct(userId, qty);
    }

    private boolean deduct(String userId, int qty) {
        return true;
    }
}
