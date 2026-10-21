package com.shop;

import org.springframework.stereotype.Service;

@Service
public class OrderService {
    private final StockService stockService;
    private final PaymentGateway paymentGateway;
    private final Notifier notifier;

    public OrderService(StockService stockService, PaymentGateway paymentGateway, Notifier notifier) {
        this.stockService = stockService;
        this.paymentGateway = paymentGateway;
        this.notifier = notifier;
    }

    /** 주문 생성: 검증 → 재고 차감 → 결제 → 알림. */
    public boolean create(String userId, String itemId, int qty) {
        validate(itemId, qty);
        stockService.decrease(itemId, qty);
        boolean paid = paymentGateway.pay(userId, itemId, qty);
        if (paid) {
            notifier.send(userId, itemId);
        }
        return paid;
    }

    private void validate(String itemId, int qty) {
        if (qty <= 0) {
            throw new IllegalArgumentException("qty");
        }
        stockService.exists(itemId);
    }
}
