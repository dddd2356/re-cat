# 쇼핑 주문 모듈

## 주문 생성 흐름

1. `OrderService.create` → `OrderService.validate`
2. `OrderService.create` → `StockService.decrease`
3. 결제는 게이트웨이로 보낸다: `OrderService.create` → `CardPaymentGateway.pay`
4. 결제가 끝나면 알림을 보낸다: `OrderService.create` → `Notifier.send`
