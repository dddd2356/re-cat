package com.shop;

import org.springframework.stereotype.Component;

@Component
public class CardPaymentGateway implements PaymentGateway {
    public boolean pay(String userId, String itemId, int qty) {
        return charge(userId, qty);
    }

    private boolean charge(String userId, int qty) {
        return true;
    }
}
