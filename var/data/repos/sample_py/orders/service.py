from .stock import StockService
from .payment import PaymentClient
from .notify import send_receipt


class OrderService:
    """주문 생성 흐름. 재고를 차감하고 결제를 요청한 뒤 영수증을 보낸다."""

    def __init__(self):
        self.stock = StockService()
        self.payment = PaymentClient()

    def create(self, user_id, item_id, qty):
        """주문 생성: 재고 차감 → 결제 요청 → 영수증 발송."""
        self.validate(item_id, qty)
        self.stock.decrease(item_id, qty)
        paid = self.payment.request(user_id, item_id, qty)
        if paid:
            send_receipt(user_id, item_id)
        return paid

    def validate(self, item_id, qty):
        if qty <= 0:
            raise ValueError("qty must be positive")
        return self.stock.exists(item_id)

    def cancel(self, user_id, item_id, qty):
        """주문 취소: 환불하고 재고를 되돌린다."""
        self.payment.refund(user_id, item_id, qty)
        self.stock.increase(item_id, qty)

    def run_hook(self, name):
        """이름으로 훅을 찾아 실행한다 (동적 호출 — 정적 분석이 따라갈 수 없음)."""
        hook = getattr(self, "hook_" + name)
        return hook()
