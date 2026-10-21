package com.shop;

public interface PaymentGateway {
    boolean pay(String userId, String itemId, int qty);
}
