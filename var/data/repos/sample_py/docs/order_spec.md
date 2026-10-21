# 주문 처리 명세 (v1.2, 2023-04 작성)

주문 서비스의 단계별 처리 흐름을 정리한 문서다. 담당자 변경 이후 갱신되지 않았다.

## 1단계: 주문 검증

`OrderService.create`가 `OrderService.validate`를 호출해 수량이 0보다 큰지, 상품이 있는지 확인한다.

## 2단계: 재고 차감

`OrderService.create` → `StockService.decrease`

재고가 부족하면 예외를 던지고 주문을 중단한다.

## 3단계: 결제 요청

`OrderService.create`가 `PaymentClient.request`를 호출한다.
결제 키는 환경 변수에서 읽는다: `OrderService.create` → `_api_key`

## 4단계: 영수증 발송

결제가 성공하면 `OrderService.create` → `send_receipt` 로 영수증 메일을 보낸다.

## 5단계: 감사 로그 기록

모든 주문은 `OrderService.create`가 `AuditLogger.write`를 호출해 감사 로그를 남긴다.

## 주문 취소

`OrderService.cancel` → `PaymentClient.refund` 로 환불한다.
취소 안내 메일도 보낸다: `OrderService.cancel` → `send_receipt`

## 확장 훅

운영자가 등록한 훅은 `OrderService.run_hook`이 실행하며, 감사 훅은 재고를 확인한다: `OrderService.run_hook` → `StockService.exists`
