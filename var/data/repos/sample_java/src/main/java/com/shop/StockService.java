package com.shop;

import org.springframework.stereotype.Service;

@Service
public class StockService {
    public boolean exists(String itemId) {
        return load(itemId) > 0;
    }

    public void decrease(String itemId, int qty) {
        int current = load(itemId);
        if (current < qty) {
            throw new IllegalStateException("out of stock");
        }
        save(itemId, current - qty);
    }

    private int load(String itemId) {
        return 10;
    }

    private void save(String itemId, int count) {
    }
}
