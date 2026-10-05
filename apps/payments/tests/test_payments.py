"""Online payments: JazzCash and Easypaisa checkout, callbacks, verification
and idempotency. Provider HTTP calls are always mocked."""

import base64
from datetime import timedelta
from decimal import Decimal
from unittest import mock
from urllib.parse import urlencode

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.tokens import issue_access_token
from apps.orders.models import Order
from apps.payments.models import Payment
from apps.payments.services import jazzcash
from apps.payments.services.base import ProviderError

ADDRESS = {
    "country": "Pakistan", "first_name": "Ali", "last_name": "Khan",
    "address1": "House 12", "city": "Islamabad", "phone": "0300 1234567",
}
PROVIDER_SETTINGS = {
    "JAZZCASH_MERCHANT_ID": "MC12345",
    "JAZZCASH_PASSWORD": "pw123",
    "JAZZCASH_INTEGRITY_SALT": "salt123",
    "JAZZCASH_RETURN_URL": "https://shop.example/api/payments/jazzcash/callback/",
    "EASYPAISA_STORE_ID": "1234",
    "EASYPAISA_HASH_KEY": "ABCDEFGHIJKLMNOP",
    "EASYPAISA_USERNAME": "store-user",
    "EASYPAISA_PASSWORD": "store-pass",
    "EASYPAISA_ACCOUNT_NUM": "987654",
    "EASYPAISA_RETURN_URL": "https://shop.example/api/payments/easypaisa/callback/",
    "PAYMENT_RESULT_URL": "https://shop.example/payment/result",
}
JC_POST = "apps.payments.services.jazzcash.post_json"
EP_POST = "apps.payments.services.easypaisa.post_json"


def jc_inquiry(code="000", **extra):
    """A JazzCash Payment Inquiry response for payment response ``code``."""
    return {"pp_ResponseCode": "000", "pp_ResponseMessage": "Successful",
            "pp_PaymentResponseCode": code, "pp_PaymentResponseMessage": "msg",
            "pp_RetreivalReferenceNo": "220101123456", **extra}


def ep_inquiry(payment, status="PAID", amount=None, **extra):
    return {"responseCode": "0000", "responseDesc": "SUCCESS", "orderId": payment.payment_id,
            "storeId": "1234", "transactionStatus": status, "transactionId": "EP998877",
            "transactionAmount": str(amount if amount is not None else payment.amount), **extra}


@override_settings(**PROVIDER_SETTINGS)
class PaymentTestBase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("buyer", "buyer@example.com", "Buyer-pass-123!")
        self.api = self.client_for(self.user)
        self.order = self.make_order(self.user)

    def client_for(self, user):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(user)}")
        return api

    def make_order(self, user, method=Order.PAYMENT_JAZZCASH, status=Order.STATUS_AWAITING_PAYMENT,
                   total="15000.00"):
        return Order.objects.create(
            number=f"PF-{Order.objects.count() + 1:08X}", user_id=user.id, status=status,
            contact="0300 1234567", delivery_method="ship", shipping_address=ADDRESS,
            billing_address=ADDRESS, shipping_method="standard", payment_method=method,
            subtotal=Decimal(total), total=Decimal(total),
        )

    def create(self, method="JAZZCASH", order=None, api=None, **extra):
        order = order or self.order
        return (api or self.api).post(
            "/api/payments/create/",
            {"order_id": order.number, "payment_method": method, **extra}, format="json",
        )

    def start(self, method="JAZZCASH", order=None):
        resp = self.create(method, order)
        self.assertIn(resp.status_code, (200, 201), resp.content)
        return Payment.objects.get(payment_id=resp.json()["data"]["payment_id"]), resp.json()["data"]

    def status(self, payment, api=None):
        return (api or self.api).get(f"/api/payments/{payment.payment_id}/")

    def jc_return_fields(self, payment, code="000", amount=None):
        """What JazzCash POSTs to the return URL, correctly signed."""
        fields = {
            "pp_Version": "1.1", "pp_TxnType": "MWALLET", "pp_Language": "EN",
            "pp_MerchantID": "MC12345", "pp_SubMerchantID": "", "pp_TxnRefNo": payment.payment_id,
            "pp_Amount": amount or jazzcash.to_paisa(payment.amount), "pp_TxnCurrency": "PKR",
            "pp_TxnDateTime": "20261004120000", "pp_BillReference": "PF00000001",
            "pp_ResponseCode": code, "pp_ResponseMessage": "Thank you", "pp_RetreivalReferenceNo": "220101123456",
            "pp_AuthCode": "123456", "pp_SettlementExpiry": "", "ppmpf_1": payment.order.number,
        }
        fields["pp_SecureHash"] = jazzcash.secure_hash(fields, "salt123")
        return fields

    def jc_return(self, payment, fields=None, inquiry=None, side_effect=None):
        with mock.patch(JC_POST, return_value=inquiry or jc_inquiry(), side_effect=side_effect) as post:
            resp = APIClient().post(
                "/api/payments/jazzcash/callback/", urlencode(fields or self.jc_return_fields(payment)),
                content_type="application/x-www-form-urlencoded",
            )
        return resp, post

    def ep_return(self, payment, inquiry=None, side_effect=None, **query):
        params = {"status": "0000", "desc": "Success", "orderRefNumber": payment.payment_id, **query}
        with mock.patch(EP_POST, return_value=inquiry, side_effect=side_effect) as post:
            resp = APIClient().get("/api/payments/easypaisa/callback/", params)
        return resp, post

    def refreshed(self, *objs):
        for obj in objs:
            obj.refresh_from_db()


class CreatePaymentTest(PaymentTestBase):
    def test_valid_order_starts_payment_with_amount_from_database(self):
        resp = self.create(amount="1.00")  # a client-sent amount is ignored
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["order_id"], self.order.number)
        self.assertEqual((data["payment_method"], data["status"]), ("JAZZCASH", "PENDING"))
        self.assertEqual((data["amount"], data["currency"]), ("15000.00", "PKR"))
        self.assertTrue(data["payment_id"].startswith("PY"))
        self.assertNotEqual(data["payment_id"], self.order.number)
        self.assertEqual(data["checkout_url"], jazzcash.SANDBOX_CHECKOUT_URL)
        self.assertEqual(data["checkout"]["fields"]["pp_Amount"], "1500000")
        payment = Payment.objects.get()
        self.assertEqual(payment.amount, Decimal("15000.00"))
        self.refreshed(self.order)
        self.assertEqual(self.order.number, data["order_id"])  # order number untouched

    def test_lowercase_method_accepted(self):
        self.assertEqual(self.create("jazzcash").status_code, 201)

    def test_invalid_order_and_method(self):
        resp = self.api.post("/api/payments/create/", {"order_id": "PF-NOPE0000", "payment_method": "JAZZCASH"},
                             format="json")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.create("PAYPAL").status_code, 400)
        self.assertEqual(self.api.post("/api/payments/create/", {}, format="json").status_code, 400)

    def test_unauthorized_order(self):
        other = User.objects.create_user("other", "o@example.com", "Other-pass-123!")
        self.assertEqual(self.create(api=self.client_for(other)).status_code, 404)
        self.assertEqual(self.create(api=APIClient()).status_code, 401)
        self.assertFalse(Payment.objects.exists())

    def test_already_paid_order(self):
        payment, _ = self.start()
        self.jc_return(payment)
        resp = self.create()
        self.assertEqual(resp.status_code, 409)
        self.assertIn("already been paid", resp.json()["message"])
        self.assertEqual(Payment.objects.count(), 1)

    def test_order_not_payable_online(self):
        cod = self.make_order(self.user, method=Order.PAYMENT_COD, status=Order.STATUS_PENDING)
        self.assertEqual(self.create(order=cod).status_code, 409)
        cancelled = self.make_order(self.user, status=Order.STATUS_CANCELLED)
        self.assertEqual(self.create(order=cancelled).status_code, 409)

    def test_bank_deposit_order_paid_online(self):
        order = self.make_order(self.user, method=Order.PAYMENT_BANK_DEPOSIT)
        payment, _ = self.start(order=order)
        self.jc_return(payment)
        self.refreshed(order)
        self.assertEqual(order.status, Order.STATUS_CONFIRMED)
        self.assertEqual(order.payment_method, Order.PAYMENT_JAZZCASH)

    @override_settings(JAZZCASH_INTEGRITY_SALT="")
    def test_unconfigured_provider_rejected(self):
        resp = self.create()
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Payment.objects.exists())


class JazzCashTest(PaymentTestBase):
    def test_secure_hash_matches_guide_example(self):
        # Guide §14.2: salt, then values in field-name order, joined with "&".
        fields = {"pp_MerchantID": "MER123", "pp_OrderInfo": "A48cvE28", "pp_Amount": "2995", "pp_Empty": ""}
        import hashlib
        import hmac
        expected = hmac.new(b"0F5DD14AE2", b"0F5DD14AE2&2995&MER123&A48cvE28", hashlib.sha256).hexdigest().upper()
        self.assertEqual(jazzcash.secure_hash(fields, "0F5DD14AE2"), expected)

    def test_payment_creation_builds_signed_form(self):
        payment, data = self.start()
        fields = data["checkout"]["fields"]
        self.assertEqual(data["checkout"]["method"], "POST")
        self.assertEqual(fields["pp_TxnRefNo"], payment.payment_id)
        self.assertEqual(fields["pp_MerchantID"], "MC12345")
        self.assertEqual(fields["pp_TxnCurrency"], "PKR")
        self.assertEqual(fields["pp_BillReference"], self.order.number.replace("-", ""))
        self.assertEqual(fields["pp_ReturnURL"], PROVIDER_SETTINGS["JAZZCASH_RETURN_URL"])
        self.assertTrue(jazzcash.has_valid_hash(fields))
        # Nothing secret is kept on the payment record.
        self.assertNotIn("pw123", str(payment.metadata))

    def test_successful_callback(self):
        payment, _ = self.start()
        resp, post = self.jc_return(payment)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"https://shop.example/payment/result?payment_id={payment.payment_id}")
        post.assert_called_once()
        sent = post.call_args.args[1]
        self.assertEqual(sent["pp_TxnRefNo"], payment.payment_id)
        self.assertTrue(jazzcash.has_valid_hash(sent))
        self.refreshed(payment, self.order)
        self.assertEqual(payment.status, "PAID")
        self.assertEqual(payment.provider_transaction_id, "220101123456")
        self.assertIsNotNone(payment.paid_at)
        self.assertEqual(self.order.status, Order.STATUS_CONFIRMED)
        self.assertNotIn("pp_SecureHash", payment.metadata["provider_response"])

    def test_failed_callback(self):
        payment, _ = self.start()
        self.jc_return(payment, self.jc_return_fields(payment, code="004"), inquiry=jc_inquiry("004"))
        self.refreshed(payment, self.order)
        self.assertEqual(payment.status, "FAILED")
        self.assertIn("004", payment.failure_reason)
        self.assertEqual(self.order.status, Order.STATUS_AWAITING_PAYMENT)
        # The customer can try again.
        self.assertEqual(self.create().status_code, 201)

    def test_cancelled_callback(self):
        payment, _ = self.start()
        self.jc_return(payment, self.jc_return_fields(payment, code="112"), inquiry=jc_inquiry("112"))
        self.refreshed(payment)
        self.assertEqual(payment.status, "CANCELLED")

    def test_invalid_signature_changes_nothing(self):
        payment, _ = self.start()
        fields = self.jc_return_fields(payment)
        fields["pp_Amount"] = "100"  # tampered after signing
        resp, post = self.jc_return(payment, fields)
        self.assertEqual(resp.status_code, 302)
        post.assert_not_called()
        self.refreshed(payment, self.order)
        self.assertEqual(payment.status, "PENDING")
        self.assertEqual(self.order.status, Order.STATUS_AWAITING_PAYMENT)

        fields = self.jc_return_fields(payment)
        fields["pp_MerchantID"] = "SOMEONEELSE"
        fields["pp_SecureHash"] = jazzcash.secure_hash(fields, "salt123")
        _, post = self.jc_return(payment, fields)
        post.assert_not_called()

    def test_invalid_amount(self):
        payment, _ = self.start()
        self.jc_return(payment, self.jc_return_fields(payment, amount="100"))
        self.refreshed(payment, self.order)
        self.assertEqual(payment.status, "FAILED")
        self.assertIn("Amount mismatch", payment.failure_reason)
        self.assertTrue(payment.metadata["needs_review"])
        self.assertEqual(self.order.status, Order.STATUS_AWAITING_PAYMENT)

    def test_duplicate_callback(self):
        payment, _ = self.start()
        self.jc_return(payment)
        self.refreshed(payment, self.order)
        paid_at, order_updated = payment.paid_at, self.order.updated_at
        resp, _ = self.jc_return(payment)  # retry / browser refresh
        self.assertEqual(resp.status_code, 302)
        # A late failure report can't undo the payment either.
        self.jc_return(payment, self.jc_return_fields(payment, code="004"), inquiry=jc_inquiry("004"))
        self.refreshed(payment, self.order)
        self.assertEqual((payment.status, payment.paid_at), ("PAID", paid_at))
        self.assertEqual(self.order.updated_at, order_updated)
        self.assertEqual(Payment.objects.filter(status="PAID").count(), 1)

    def test_provider_error_waits_for_inquiry(self):
        payment, _ = self.start()
        self.jc_return(payment, side_effect=ProviderError("timeout"))
        self.refreshed(payment, self.order)
        # The signed browser result alone never marks the payment paid.
        self.assertEqual(payment.status, "PROCESSING")
        self.assertEqual(self.order.status, Order.STATUS_AWAITING_PAYMENT)

        with mock.patch(JC_POST, return_value=jc_inquiry()):
            resp = self.status(payment)
        self.assertEqual(resp.json()["data"]["status"], "PAID")
        self.refreshed(self.order)
        self.assertEqual(self.order.status, Order.STATUS_CONFIRMED)

    def test_inquiry_error_response_is_a_provider_error(self):
        payment, _ = self.start()
        with mock.patch(JC_POST, return_value={"pp_ResponseCode": "101", "pp_ResponseMessage": "Invalid"}):
            with self.assertRaises(ProviderError):
                jazzcash.verify(payment)
        bad_hash = jc_inquiry(pp_SecureHash="DEADBEEF")
        with mock.patch(JC_POST, return_value=bad_hash):
            with self.assertRaises(ProviderError):
                jazzcash.verify(payment)

    def test_ipn(self):
        payment, _ = self.start()
        fields = self.jc_return_fields(payment)
        with mock.patch(JC_POST, return_value=jc_inquiry()):
            resp = APIClient().post("/api/payments/jazzcash/ipn/", fields, format="json")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["pp_ResponseCode"], "000")
        self.assertTrue(jazzcash.has_valid_hash(body))
        self.refreshed(payment)
        self.assertEqual(payment.status, "PAID")

        fields["pp_SecureHash"] = "0" * 64
        resp = APIClient().post("/api/payments/jazzcash/ipn/", fields, format="json")
        self.assertEqual((resp.status_code, resp.json()["pp_ResponseCode"]), (400, "115"))


def decrypt_hashed_request(value, key=b"ABCDEFGHIJKLMNOP"):
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = decryptor.update(base64.b64decode(value)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode()


class EasypaisaTest(PaymentTestBase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order(self.user, method=Order.PAYMENT_EASYPAISA, total="199.50")

    def test_payment_creation_builds_hosted_checkout(self):
        payment, data = self.start("EASYPAISA")
        fields = data["checkout"]["fields"]
        self.assertEqual(data["checkout_url"], "https://easypaystg.easypaisa.com.pk/easypay/Index.jsf")
        self.assertEqual(fields["amount"], "199.5")
        self.assertEqual(fields["orderRefNum"], payment.payment_id)
        self.assertEqual(fields["mobileNum"], "03001234567")
        self.assertEqual(fields["paymentMethod"], "MA_PAYMENT_METHOD")
        plain = decrypt_hashed_request(fields["merchantHashedReq"])
        self.assertTrue(plain.startswith("amount=199.5&autoRedirect=1&expiryDate="))
        self.assertIn(f"&orderRefNum={payment.payment_id}&paymentMethod=MA_PAYMENT_METHOD&", plain)
        self.assertTrue(plain.endswith("&storeId=1234"))
        self.assertNotIn("emailAddr", plain)  # empty fields are left out

    def test_auth_token_is_forwarded_to_confirm(self):
        resp = APIClient().get("/api/payments/easypaisa/callback/", {"auth_token": "abc123=="})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('action="https://easypaystg.easypaisa.com.pk/easypay/Confirm.jsf"', html)
        self.assertIn('name="auth_token" value="abc123=="', html)
        self.assertIn(PROVIDER_SETTINGS["EASYPAISA_RETURN_URL"], html)
        bad = APIClient().get("/api/payments/easypaisa/callback/", {"auth_token": '"><script>'})
        self.assertEqual(bad.status_code, 400)

    def test_successful_callback(self):
        payment, _ = self.start("EASYPAISA")
        resp, post = self.ep_return(payment, inquiry=ep_inquiry(payment))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(f"payment_id={payment.payment_id}", resp["Location"])
        url, body = post.call_args.args
        self.assertTrue(url.endswith("/easypay-service/rest/v4/inquire-transaction"))
        self.assertEqual(body, {"orderId": payment.payment_id, "storeId": "1234", "accountNum": "987654"})
        credentials = post.call_args.kwargs["headers"]["Credentials"]
        self.assertEqual(base64.b64decode(credentials).decode(), "store-user:store-pass")
        self.refreshed(payment, self.order)
        self.assertEqual((payment.status, payment.provider_transaction_id), ("PAID", "EP998877"))
        self.assertEqual(self.order.status, Order.STATUS_CONFIRMED)

    def test_failed_callback(self):
        payment, _ = self.start("EASYPAISA")
        self.ep_return(payment, inquiry=ep_inquiry(payment, status="FAILED"), status="0001")
        self.refreshed(payment, self.order)
        self.assertEqual(payment.status, "FAILED")
        self.assertEqual(self.order.status, Order.STATUS_AWAITING_PAYMENT)

    def test_unverified_success_redirect_is_not_trusted(self):
        # The postBack query says success, but Easypaisa's API doesn't.
        payment, _ = self.start("EASYPAISA")
        self.ep_return(payment, inquiry=ep_inquiry(payment, status="PENDING"), status="0000")
        self.refreshed(payment, self.order)
        self.assertEqual(payment.status, "PROCESSING")
        self.assertEqual(self.order.status, Order.STATUS_AWAITING_PAYMENT)
        # Unknown or malformed references don't reach any payment.
        resp, post = self.ep_return(payment, inquiry=None, orderRefNumber="PF-NOT-A-PAYMENT")
        post.assert_not_called()
        self.assertIn("error=unknown_payment", resp["Location"])

    def test_inquiry_authentication_failure(self):
        payment, _ = self.start("EASYPAISA")
        self.ep_return(payment, inquiry={"responseCode": "0001", "responseDesc": "Invalid credentials"})
        self.refreshed(payment)
        self.assertEqual(payment.status, "PENDING")
        self.assertEqual(payment.metadata["last_check"], "error")

    def test_invalid_amount(self):
        payment, _ = self.start("EASYPAISA")
        self.ep_return(payment, inquiry=ep_inquiry(payment, amount="1.0"))
        self.refreshed(payment, self.order)
        self.assertEqual(payment.status, "FAILED")
        self.assertIn("Amount mismatch", payment.failure_reason)
        self.assertEqual(self.order.status, Order.STATUS_AWAITING_PAYMENT)

    def test_duplicate_callback_and_ipn(self):
        payment, _ = self.start("EASYPAISA")
        self.ep_return(payment, inquiry=ep_inquiry(payment))
        with mock.patch(EP_POST, return_value=ep_inquiry(payment)) as post:
            resp = APIClient().post("/api/payments/easypaisa/ipn/", {
                "url": f"https://easypay.easypaisa.com.pk/easypay-service/rest/v1/order-status/1234/{payment.payment_id}",
            }, format="json")
        self.assertEqual(resp.status_code, 200)
        post.assert_not_called()  # already PAID: nothing to verify again
        self.assertEqual(Payment.objects.filter(status="PAID").count(), 1)

    def test_ipn_settles_payment(self):
        payment, _ = self.start("EASYPAISA")
        with mock.patch(EP_POST, return_value=ep_inquiry(payment)):
            resp = APIClient().get("/api/payments/easypaisa/ipn/", {"orderRefNumber": payment.payment_id})
        self.assertEqual(resp.status_code, 200)
        self.refreshed(payment)
        self.assertEqual(payment.status, "PAID")
        resp = APIClient().get("/api/payments/easypaisa/ipn/", {"orderRefNumber": "PY0000000000000000"})
        self.assertEqual(resp.status_code, 404)

    def test_provider_error(self):
        payment, _ = self.start("EASYPAISA")
        resp, _ = self.ep_return(payment, side_effect=ProviderError("HTTP 503"))
        self.assertEqual(resp.status_code, 302)
        self.refreshed(payment)
        self.assertEqual(payment.status, "PENDING")

    @override_settings(EASYPAISA_HASH_KEY="short")
    def test_bad_hash_key_rolls_back_payment(self):
        resp = self.create("EASYPAISA")
        self.assertEqual(resp.status_code, 502)
        self.assertFalse(Payment.objects.exists())


class IdempotencyAndStatusTest(PaymentTestBase):
    def test_multiple_pay_now_requests_reuse_the_payment(self):
        first = self.create()
        second = self.create()
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.json()["data"]["payment_id"], second.json()["data"]["payment_id"])
        self.assertEqual(first.json()["data"]["checkout"], second.json()["data"]["checkout"])
        self.assertEqual(Payment.objects.count(), 1)

    def test_switching_method_checks_and_replaces_the_open_attempt(self):
        old, _ = self.start("JAZZCASH")
        with mock.patch(JC_POST, return_value={"pp_ResponseCode": "109", "pp_ResponseMessage": "Not found"}):
            new, _ = self.start("EASYPAISA")
        self.refreshed(old)
        self.assertEqual(old.status, "CANCELLED")
        self.assertNotEqual(old.payment_id, new.payment_id)
        self.assertEqual(new.payment_method, "EASYPAISA")

    def test_switching_method_finds_the_old_attempt_paid(self):
        self.start("JAZZCASH")
        with mock.patch(JC_POST, return_value=jc_inquiry()):
            resp = self.create("EASYPAISA")
        self.assertEqual(resp.status_code, 409)
        self.refreshed(self.order)
        self.assertEqual(self.order.status, Order.STATUS_CONFIRMED)
        self.assertEqual(Payment.objects.count(), 1)

    def test_cannot_replace_an_attempt_being_processed(self):
        self.start("JAZZCASH")
        with mock.patch(JC_POST, return_value=jc_inquiry("157")):
            resp = self.create("EASYPAISA")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("still being processed", resp.json()["message"])

    def test_payment_timeout(self):
        payment, _ = self.start()
        Payment.objects.filter(pk=payment.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        with mock.patch(JC_POST, return_value={"pp_ResponseCode": "109", "pp_ResponseMessage": "Not found"}):
            resp = self.status(payment)
        self.assertEqual(resp.json()["data"]["status"], "EXPIRED")
        # Pay Now again opens a fresh attempt.
        new, _ = self.start()
        self.assertNotEqual(new.payment_id, payment.payment_id)

    def test_expired_attempt_is_replaced_on_pay_now(self):
        payment, _ = self.start()
        Payment.objects.filter(pk=payment.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        with mock.patch(JC_POST, side_effect=ProviderError("down")):
            new, _ = self.start()
        self.refreshed(payment)
        self.assertEqual(payment.status, "EXPIRED")
        self.assertNotEqual(new.payment_id, payment.payment_id)

    def test_callback_after_frontend_gave_up(self):
        # The customer paid an attempt that was later replaced: the late,
        # verified success still confirms the order.
        old, _ = self.start("JAZZCASH")
        with mock.patch(JC_POST, return_value={"pp_ResponseCode": "109", "pp_ResponseMessage": "Not found"}):
            self.start("EASYPAISA")
        self.jc_return(old)
        self.refreshed(old, self.order)
        self.assertEqual(old.status, "PAID")
        self.assertEqual(self.order.status, Order.STATUS_CONFIRMED)
        self.assertEqual(self.order.payment_method, Order.PAYMENT_JAZZCASH)

    def test_second_real_charge_is_flagged_for_refund(self):
        old, _ = self.start("JAZZCASH")
        with mock.patch(JC_POST, return_value={"pp_ResponseCode": "109", "pp_ResponseMessage": "Not found"}):
            new, _ = self.start("EASYPAISA")
        self.ep_return(new, inquiry=ep_inquiry(new))
        self.jc_return(old)
        self.refreshed(old, self.order)
        self.assertEqual(old.status, "PAID")
        self.assertTrue(old.metadata["needs_refund"])
        self.assertEqual(self.order.status, Order.STATUS_CONFIRMED)
        self.assertEqual(self.order.payment_method, Order.PAYMENT_EASYPAISA)

    def test_provider_transaction_cannot_settle_two_payments(self):
        first, _ = self.start()
        self.jc_return(first)
        other_order = self.make_order(self.user)
        second, _ = self.start(order=other_order)
        self.jc_return(second)  # same retrieval reference as the first
        self.refreshed(second, other_order)
        self.assertEqual(second.status, "FAILED")
        self.assertIn("already belongs", second.failure_reason)
        self.assertEqual(other_order.status, Order.STATUS_AWAITING_PAYMENT)

    def test_status_api(self):
        payment, _ = self.start()
        with mock.patch(JC_POST, return_value={"pp_ResponseCode": "109", "pp_ResponseMessage": "Not found"}) as post:
            resp = self.status(payment)
            self.status(payment)  # polled again straight away
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(post.call_count, 1)  # provider asked at most every few seconds
        data = resp.json()["data"]
        self.assertEqual(
            {k: data[k] for k in ("payment_id", "order_id", "payment_method", "amount", "currency", "status")},
            {"payment_id": payment.payment_id, "order_id": self.order.number, "payment_method": "JAZZCASH",
             "amount": "15000.00", "currency": "PKR", "status": "PENDING"},
        )
        other = User.objects.create_user("other", "o@example.com", "Other-pass-123!")
        self.assertEqual(self.status(payment, api=self.client_for(other)).status_code, 404)
        self.assertEqual(self.status(payment, api=APIClient()).status_code, 401)

    def test_paid_payment_status_needs_no_provider_call(self):
        payment, _ = self.start()
        self.jc_return(payment)
        with mock.patch(JC_POST) as post:
            resp = self.status(payment)
        post.assert_not_called()
        self.assertEqual(resp.json()["data"]["status"], "PAID")

    def test_database_error_rolls_back_payment_and_order(self):
        payment, _ = self.start()
        with mock.patch.object(Order, "save", side_effect=RuntimeError("db down")):
            with self.assertRaises(RuntimeError):
                self.jc_return(payment)
        self.refreshed(payment, self.order)
        self.assertEqual(payment.status, "PENDING")
        self.assertEqual(self.order.status, Order.STATUS_AWAITING_PAYMENT)


@override_settings(**PROVIDER_SETTINGS)
class CheckoutIntegrationTest(TestCase):
    def test_checkout_options_list_configured_providers(self):
        data = APIClient().get("/api/orders/checkout-options/").json()["data"]
        codes = [m["code"] for m in data["payment_methods"]]
        self.assertEqual(codes, ["cod", "bank_deposit", "jazzcash", "easypaisa"])

    @override_settings(EASYPAISA_STORE_ID="")
    def test_unconfigured_provider_not_offered(self):
        data = APIClient().get("/api/orders/checkout-options/").json()["data"]
        self.assertNotIn("easypaisa", [m["code"] for m in data["payment_methods"]])
