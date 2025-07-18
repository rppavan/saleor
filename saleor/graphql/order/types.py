import logging
from decimal import Decimal
from uuid import UUID

import graphene
import prices
from django.core.exceptions import ValidationError
from graphene import relay
from promise import Promise

from ...account.models import Address
from ...account.models import User as UserModel
from ...checkout.utils import get_external_shipping_id
from ...core.anonymize import obfuscate_address, obfuscate_email
from ...core.db.connection import allow_writer_in_context
from ...core.prices import quantize_price
from ...core.taxes import zero_money
from ...discount import DiscountType
from ...discount import models as discount_models
from ...graphql.checkout.types import DeliveryMethod
from ...graphql.core.context import (
    SyncWebhookControlContext,
    get_database_connection_name,
)
from ...graphql.core.federation.entities import federated_entity
from ...graphql.core.federation.resolvers import resolve_federation_references
from ...graphql.order.resolvers import resolve_orders
from ...graphql.utils import get_user_or_app_from_context
from ...graphql.warehouse.dataloaders import StockByIdLoader, WarehouseByIdLoader
from ...order import OrderOrigin, OrderStatus, calculations, models
from ...order.calculations import fetch_order_prices_if_expired
from ...order.models import FulfillmentStatus
from ...order.utils import (
    get_order_country,
    get_valid_collection_points_for_order,
    get_valid_shipping_methods_for_order,
)
from ...payment import ChargeStatus, TransactionKind
from ...payment.dataloaders import PaymentsByOrderIdLoader
from ...payment.model_helpers import get_last_payment, get_total_authorized
from ...permission.auth_filters import AuthorizationFilters, is_app, is_staff_user
from ...permission.enums import (
    AccountPermissions,
    AppPermission,
    OrderPermissions,
    PaymentPermissions,
    ProductPermissions,
)
from ...permission.utils import has_one_of_permissions
from ...product.models import ALL_PRODUCTS_PERMISSIONS, ProductMedia, ProductMediaTypes
from ...shipping.interface import ShippingMethodData
from ...shipping.utils import convert_to_shipping_method_data
from ...tax.utils import get_display_gross_prices
from ...thumbnail.utils import (
    get_image_or_proxy_url,
    get_thumbnail_format,
    get_thumbnail_size,
)
from ..account.dataloaders import AddressByIdLoader, UserByUserIdLoader
from ..account.types import User
from ..account.utils import (
    check_is_owner_or_has_one_of_perms,
    is_owner_or_has_one_of_perms,
)
from ..app.dataloaders import AppByIdLoader
from ..app.types import App
from ..channel.dataloaders import ChannelByIdLoader, ChannelByOrderIdLoader
from ..channel.types import Channel
from ..checkout.utils import prevent_sync_event_circular_query
from ..core.connection import CountableConnection
from ..core.context import ChannelContext
from ..core.descriptions import (
    ADDED_IN_318,
    ADDED_IN_319,
    ADDED_IN_320,
    ADDED_IN_321,
    DEPRECATED_IN_3X_INPUT,
    PREVIEW_FEATURE,
)
from ..core.doc_category import DOC_CATEGORY_ORDERS
from ..core.enums import LanguageCodeEnum
from ..core.fields import PermissionsField
from ..core.mutations import validation_error_to_error_type
from ..core.scalars import DateTime, PositiveDecimal
from ..core.tracing import traced_resolver
from ..core.types import (
    BaseObjectType,
    Image,
    ModelObjectType,
    Money,
    NonNullList,
    OrderError,
    TaxedMoney,
    ThumbnailField,
    Weight,
)
from ..core.types.sync_webhook_control import SyncWebhookControlContextModelObjectType
from ..core.utils import str_to_enum
from ..decorators import one_of_permissions_required
from ..discount.dataloaders import (
    OrderDiscountsByOrderIDLoader,
    OrderLineDiscountsByOrderLineIDLoader,
    VoucherByIdLoader,
)
from ..discount.enums import DiscountValueTypeEnum
from ..discount.types import Voucher
from ..giftcard.dataloaders import GiftCardsByOrderIdLoader
from ..giftcard.types import GiftCard
from ..invoice.dataloaders import InvoicesByOrderIdLoader
from ..invoice.types import Invoice
from ..meta.resolvers import check_private_metadata_privilege, resolve_metadata
from ..meta.types import MetadataItem, ObjectWithMetadata
from ..payment.dataloaders import (
    TransactionByPaymentIdLoader,
    TransactionItemByIDLoader,
)
from ..payment.enums import OrderAction
from ..payment.types import (
    Payment,
    PaymentChargeStatusEnum,
    TransactionEvent,
    TransactionItem,
)
from ..plugins.dataloaders import (
    get_plugin_manager_promise,
    plugin_manager_promise_callback,
)
from ..product.dataloaders import (
    ImagesByProductIdLoader,
    MediaByProductVariantIdLoader,
    ProductByVariantIdLoader,
    ProductChannelListingByProductIdAndChannelSlugLoader,
    ProductVariantByIdLoader,
    ThumbnailByProductMediaIdSizeAndFormatLoader,
)
from ..product.types import DigitalContentUrl, ProductVariant
from ..shipping.dataloaders import (
    ShippingMethodByIdLoader,
    ShippingMethodChannelListingByChannelSlugLoader,
    ShippingMethodChannelListingByShippingMethodIdAndChannelSlugLoader,
)
from ..shipping.types import ShippingMethod
from ..tax.dataloaders import (
    TaxClassByIdLoader,
    TaxConfigurationByChannelId,
    TaxConfigurationPerCountryByTaxConfigurationIDLoader,
)
from ..tax.types import TaxClass
from ..warehouse.types import Allocation, Stock, Warehouse
from .dataloaders import (
    AllocationsByOrderLineIdLoader,
    FulfillmentLinesByFulfillmentIdLoader,
    FulfillmentLinesByIdLoader,
    FulfillmentsByOrderIdLoader,
    OrderByIdLoader,
    OrderByNumberLoader,
    OrderEventsByIdLoader,
    OrderEventsByOrderIdLoader,
    OrderGrantedRefundLinesByOrderGrantedRefundIdLoader,
    OrderGrantedRefundsByOrderIdLoader,
    OrderLineByIdLoader,
    OrderLinesByOrderIdLoader,
    TransactionEventsByOrderGrantedRefundIdLoader,
    TransactionItemsByOrderIDLoader,
)
from .enums import (
    FulfillmentStatusEnum,
    OrderAuthorizeStatusEnum,
    OrderChargeStatusEnum,
    OrderEventsEmailsEnum,
    OrderEventsEnum,
    OrderGrantedRefundStatusEnum,
    OrderOriginEnum,
    OrderStatusEnum,
)
from .utils import validate_draft_order

logger = logging.getLogger(__name__)


def get_order_discount_event(discount_obj: dict):
    currency = discount_obj["currency"]

    amount = prices.Money(Decimal(discount_obj["amount_value"]), currency)

    old_amount = None
    old_amount_value = discount_obj.get("old_amount_value")
    if old_amount_value:
        old_amount = prices.Money(Decimal(old_amount_value), currency)

    return OrderEventDiscountObject(
        value=discount_obj.get("value"),
        amount=amount,
        value_type=discount_obj.get("value_type"),
        reason=discount_obj.get("reason"),
        old_value_type=discount_obj.get("old_value_type"),
        old_value=discount_obj.get("old_value"),
        old_amount=old_amount,
    )


def get_payment_status_for_order(
    order, granted_refunds: list[models.OrderGrantedRefund]
):
    zero_price = zero_money(order.currency)
    total_granted = sum(
        [granted_refund.amount for granted_refund in granted_refunds],
        zero_price,
    )
    charged_money = order.total_charged
    current_order_total = quantize_price(
        order.total.gross - total_granted, order.currency
    )

    if charged_money == zero_price and current_order_total <= zero_price:
        status = ChargeStatus.FULLY_CHARGED
    elif charged_money >= current_order_total:
        status = ChargeStatus.FULLY_CHARGED
    elif charged_money < current_order_total and charged_money > zero_price:
        status = ChargeStatus.PARTIALLY_CHARGED
    else:
        status = ChargeStatus.NOT_CHARGED
    return status


class OrderGrantedRefundLine(
    SyncWebhookControlContextModelObjectType[
        ModelObjectType[models.OrderGrantedRefundLine]
    ]
):
    id = graphene.GlobalID(required=True)
    quantity = graphene.Int(description="Number of items to refund.", required=True)
    order_line = graphene.Field(
        "saleor.graphql.order.types.OrderLine",
        description="Line of the order associated with this granted refund.",
        required=True,
    )
    reason = graphene.String(description="Reason for refunding the line.")

    class Meta:
        default_resolver = (
            SyncWebhookControlContextModelObjectType.resolver_with_context
        )
        description = "Represents granted refund line."
        model = models.OrderGrantedRefundLine

    @staticmethod
    def resolve_order_line(
        root: SyncWebhookControlContext[models.OrderGrantedRefundLine], info
    ):
        def _wrap_with_sync_webhook_control_context(line):
            return SyncWebhookControlContext(
                node=line, allow_sync_webhooks=root.allow_sync_webhooks
            )

        return (
            OrderLineByIdLoader(info.context)
            .load(root.node.order_line_id)
            .then(_wrap_with_sync_webhook_control_context)
        )


class OrderGrantedRefund(
    SyncWebhookControlContextModelObjectType[ModelObjectType[models.OrderGrantedRefund]]
):
    id = graphene.GlobalID(required=True)
    created_at = DateTime(required=True, description="Time of creation.")
    updated_at = DateTime(required=True, description="Time of last update.")
    amount = graphene.Field(Money, required=True, description="Refund amount.")
    reason = graphene.String(description="Reason of the refund.")
    user = graphene.Field(
        User,
        description=(
            "User who performed the action. Requires of of the following "
            f"permissions: {AccountPermissions.MANAGE_USERS.name}, "
            f"{AccountPermissions.MANAGE_STAFF.name}, "
            f"{AuthorizationFilters.OWNER.name}."
        ),
    )
    app = graphene.Field(App, description=("App that performed the action."))
    shipping_costs_included = graphene.Boolean(
        required=True,
        description=(
            "If true, the refunded amount includes the shipping price."
            "If false, the refunded amount does not include the shipping price."
        ),
    )
    lines = NonNullList(
        OrderGrantedRefundLine, description="Lines assigned to the granted refund."
    )
    status = graphene.Field(
        OrderGrantedRefundStatusEnum,
        required=True,
        description=(
            "Status of the granted refund calculated based on transactionItem assigned "
            "to granted refund." + ADDED_IN_320
        ),
    )
    transaction_events = NonNullList(
        TransactionEvent,
        description=(
            "List of refund events associated with the granted refund." + ADDED_IN_320
        ),
    )

    transaction = graphene.Field(
        TransactionItem,
        description="The transaction assigned to the granted refund." + ADDED_IN_320,
    )

    class Meta:
        default_resolver = (
            SyncWebhookControlContextModelObjectType.resolver_with_context
        )
        description = "The details of granted refund."
        model = models.OrderGrantedRefund

    @staticmethod
    def resolve_user(root: SyncWebhookControlContext[models.OrderGrantedRefund], info):
        def _resolve_user(event_user: UserModel):
            requester = get_user_or_app_from_context(info.context)
            if not requester:
                return None
            if (
                requester == event_user
                or requester.has_perm(AccountPermissions.MANAGE_USERS)
                or requester.has_perm(AccountPermissions.MANAGE_STAFF)
            ):
                return event_user
            return None

        granted_refund = root.node
        if not granted_refund.user_id:
            return None

        return (
            UserByUserIdLoader(info.context)
            .load(granted_refund.user_id)
            .then(_resolve_user)
        )

    @staticmethod
    def resolve_app(root: SyncWebhookControlContext[models.OrderGrantedRefund], info):
        granted_refund = root.node
        if granted_refund.app_id:
            return AppByIdLoader(info.context).load(granted_refund.app_id)
        return None

    @staticmethod
    def resolve_lines(root: SyncWebhookControlContext[models.OrderGrantedRefund], info):
        def _wrap_with_sync_webhook_control_context(lines):
            return [
                SyncWebhookControlContext(
                    line, allow_sync_webhooks=root.allow_sync_webhooks
                )
                for line in lines
            ]

        return (
            OrderGrantedRefundLinesByOrderGrantedRefundIdLoader(info.context)
            .load(root.node.id)
            .then(_wrap_with_sync_webhook_control_context)
        )

    @staticmethod
    @one_of_permissions_required(
        [OrderPermissions.MANAGE_ORDERS, PaymentPermissions.HANDLE_PAYMENTS]
    )
    def resolve_transaction_events(
        root: SyncWebhookControlContext[models.OrderGrantedRefund], info
    ):
        return TransactionEventsByOrderGrantedRefundIdLoader(info.context).load(
            root.node.id
        )

    @staticmethod
    @one_of_permissions_required(
        [OrderPermissions.MANAGE_ORDERS, PaymentPermissions.HANDLE_PAYMENTS]
    )
    def resolve_transaction(
        root: SyncWebhookControlContext[models.OrderGrantedRefund], info
    ):
        granted_refund = root.node
        if not granted_refund.transaction_item_id:
            return None
        return TransactionItemByIDLoader(info.context).load(
            granted_refund.transaction_item_id
        )


class OrderDiscount(BaseObjectType):
    value_type = graphene.Field(
        DiscountValueTypeEnum,
        required=True,
        description="Type of the discount: fixed or percent.",
    )
    value = PositiveDecimal(
        required=True,
        description="Value of the discount. Can store fixed value or percent value.",
    )
    reason = graphene.String(
        required=False, description="Explanation for the applied discount."
    )
    amount = graphene.Field(Money, description="Returns amount of discount.")

    class Meta:
        doc_category = DOC_CATEGORY_ORDERS


class OrderEventDiscountObject(OrderDiscount):
    old_value_type = graphene.Field(
        DiscountValueTypeEnum,
        required=False,
        description="Type of the discount: fixed or percent.",
    )
    old_value = PositiveDecimal(
        required=False,
        description="Value of the discount. Can store fixed value or percent value.",
    )
    old_amount = graphene.Field(
        Money, required=False, description="Returns amount of discount."
    )

    class Meta:
        doc_category = DOC_CATEGORY_ORDERS


class OrderEventOrderLineObject(BaseObjectType):
    quantity = graphene.Int(description="The variant quantity.")
    order_line = graphene.Field(lambda: OrderLine, description="The order line.")
    item_name = graphene.String(description="The variant name.")
    discount = graphene.Field(
        OrderEventDiscountObject, description="The discount applied to the order line."
    )

    class Meta:
        doc_category = DOC_CATEGORY_ORDERS


class OrderEvent(
    SyncWebhookControlContextModelObjectType[ModelObjectType[models.OrderEvent]]
):
    id = graphene.GlobalID(
        required=True, description="ID of the event associated with an order."
    )
    date = DateTime(description="Date when event happened at in ISO 8601 format.")
    type = OrderEventsEnum(description="Order event type.")
    user = graphene.Field(User, description="User who performed the action.")
    app = graphene.Field(
        App,
        description=(
            "App that performed the action. Requires of of the following permissions: "
            f"{AppPermission.MANAGE_APPS.name}, {OrderPermissions.MANAGE_ORDERS.name}, "
            f"{AuthorizationFilters.OWNER.name}."
        ),
    )
    message = graphene.String(description="Content of the event.")
    email = graphene.String(description="Email of the customer.")
    email_type = OrderEventsEmailsEnum(
        description="Type of an email sent to the customer."
    )
    amount = graphene.Float(description="Amount of money.")
    payment_id = graphene.String(
        description="The payment reference from the payment provider."
    )
    payment_gateway = graphene.String(description="The payment gateway of the payment.")
    quantity = graphene.Int(description="Number of items.")
    composed_id = graphene.String(description="Composed ID of the Fulfillment.")
    order_number = graphene.String(description="User-friendly number of an order.")
    invoice_number = graphene.String(
        description="Number of an invoice related to the order."
    )
    oversold_items = NonNullList(
        graphene.String, description="List of oversold lines names."
    )
    lines = NonNullList(OrderEventOrderLineObject, description="The concerned lines.")
    fulfilled_items = NonNullList(
        lambda: FulfillmentLine, description="The lines fulfilled."
    )
    warehouse = graphene.Field(
        Warehouse, description="The warehouse were items were restocked."
    )
    transaction_reference = graphene.String(
        description="The transaction reference of captured payment."
    )
    shipping_costs_included = graphene.Boolean(
        description="Define if shipping costs were included to the refund."
    )
    related_order = graphene.Field(
        lambda: Order, description="The order which is related to this order."
    )
    related = graphene.Field(
        lambda: OrderEvent,
        description="The order event which is related to this event.",
    )
    discount = graphene.Field(
        OrderEventDiscountObject, description="The discount applied to the order."
    )
    reference = graphene.String(description="The reference of payment's transaction.")

    class Meta:
        default_resolver = (
            SyncWebhookControlContextModelObjectType.resolver_with_context
        )
        description = "History log of the order."
        model = models.OrderEvent
        interfaces = [relay.Node]

    @staticmethod
    def resolve_user(root: SyncWebhookControlContext[models.OrderEvent], info):
        event = root.node
        user_or_app = get_user_or_app_from_context(info.context)
        if not user_or_app:
            return None
        requester = user_or_app

        def _resolve_user(event_user):
            if (
                requester == event_user
                or requester.has_perm(AccountPermissions.MANAGE_USERS)
                or requester.has_perm(AccountPermissions.MANAGE_STAFF)
            ):
                return event_user
            return None

        if not event.user_id:
            return None

        return UserByUserIdLoader(info.context).load(event.user_id).then(_resolve_user)

    @staticmethod
    def resolve_app(root: SyncWebhookControlContext[models.OrderEvent], info):
        requestor = get_user_or_app_from_context(info.context)
        event = root.node

        def _resolve_app(user):
            check_is_owner_or_has_one_of_perms(
                requestor,
                user,
                AppPermission.MANAGE_APPS,
                OrderPermissions.MANAGE_ORDERS,
            )
            return (
                AppByIdLoader(info.context).load(event.app_id) if event.app_id else None
            )

        if event.user_id:
            return (
                UserByUserIdLoader(info.context).load(event.user_id).then(_resolve_app)
            )
        return _resolve_app(None)

    @staticmethod
    def resolve_email(root: SyncWebhookControlContext[models.OrderEvent], _info):
        return root.node.parameters.get("email", None)

    @staticmethod
    def resolve_email_type(root: SyncWebhookControlContext[models.OrderEvent], _info):
        return root.node.parameters.get("email_type", None)

    @staticmethod
    def resolve_amount(root: SyncWebhookControlContext[models.OrderEvent], _info):
        amount = root.node.parameters.get("amount", None)
        return float(amount) if amount else None

    @staticmethod
    def resolve_payment_id(root: SyncWebhookControlContext[models.OrderEvent], _info):
        return root.node.parameters.get("payment_id", None)

    @staticmethod
    def resolve_payment_gateway(
        root: SyncWebhookControlContext[models.OrderEvent], _info
    ):
        return root.node.parameters.get("payment_gateway", None)

    @staticmethod
    def resolve_quantity(root: SyncWebhookControlContext[models.OrderEvent], _info):
        quantity = root.node.parameters.get("quantity", None)
        return int(quantity) if quantity else None

    @staticmethod
    def resolve_message(root: SyncWebhookControlContext[models.OrderEvent], _info):
        return root.node.parameters.get("message", None)

    @staticmethod
    def resolve_composed_id(root: SyncWebhookControlContext[models.OrderEvent], _info):
        return root.node.parameters.get("composed_id", None)

    @staticmethod
    def resolve_oversold_items(
        root: SyncWebhookControlContext[models.OrderEvent], _info
    ):
        return root.node.parameters.get("oversold_items", None)

    @staticmethod
    def resolve_order_number(root: SyncWebhookControlContext[models.OrderEvent], info):
        def _resolve_order_number(order: models.Order):
            return order.number

        return (
            OrderByIdLoader(info.context)
            .load(root.node.order_id)
            .then(_resolve_order_number)
        )

    @staticmethod
    def resolve_invoice_number(
        root: SyncWebhookControlContext[models.OrderEvent], _info
    ):
        return root.node.parameters.get("invoice_number")

    @staticmethod
    @traced_resolver
    def resolve_lines(root: SyncWebhookControlContext[models.OrderEvent], info):
        raw_lines = root.node.parameters.get("lines", None)

        if not raw_lines:
            return None

        line_pks = []
        for entry in raw_lines:
            line_pk = entry.get("line_pk", None)
            if line_pk:
                line_pks.append(UUID(line_pk))

        def _resolve_lines(lines):
            results = []
            lines_dict = {str(line.pk): line for line in lines if line}
            for raw_line in raw_lines:
                line_pk = raw_line.get("line_pk")
                line_object = lines_dict.get(line_pk)
                discount = raw_line.get("discount")
                if discount:
                    discount = get_order_discount_event(discount)
                order_line = None
                if line_object:
                    order_line = SyncWebhookControlContext(
                        line_object, allow_sync_webhooks=root.allow_sync_webhooks
                    )
                results.append(
                    OrderEventOrderLineObject(
                        quantity=raw_line["quantity"],
                        order_line=order_line,
                        item_name=raw_line["item"],
                        discount=discount,
                    )
                )

            return results

        return (
            OrderLineByIdLoader(info.context).load_many(line_pks).then(_resolve_lines)
        )

    @staticmethod
    def resolve_fulfilled_items(
        root: SyncWebhookControlContext[models.OrderEvent], info
    ):
        fulfillment_lines_ids = root.node.parameters.get("fulfilled_items", [])

        if not fulfillment_lines_ids:
            return None

        def _wrap_with_sync_webhook_control_context(lines):
            return [
                SyncWebhookControlContext(
                    node=line, allow_sync_webhooks=root.allow_sync_webhooks
                )
                for line in lines
            ]

        return (
            FulfillmentLinesByIdLoader(info.context)
            .load_many(fulfillment_lines_ids)
            .then(_wrap_with_sync_webhook_control_context)
        )

    @staticmethod
    def resolve_warehouse(root: SyncWebhookControlContext[models.OrderEvent], info):
        if warehouse_pk := root.node.parameters.get("warehouse"):
            return WarehouseByIdLoader(info.context).load(UUID(warehouse_pk))
        return None

    @staticmethod
    def resolve_transaction_reference(
        root: SyncWebhookControlContext[models.OrderEvent], _info
    ):
        return root.node.parameters.get("transaction_reference")

    @staticmethod
    def resolve_shipping_costs_included(
        root: SyncWebhookControlContext[models.OrderEvent], _info
    ):
        return root.node.parameters.get("shipping_costs_included")

    @staticmethod
    def resolve_related_order(root: SyncWebhookControlContext[models.OrderEvent], info):
        order_pk_or_number = root.node.parameters.get("related_order_pk")
        if not order_pk_or_number:
            return None

        def _wrap_with_sync_webhook_control_context(order):
            if not order:
                return None
            return SyncWebhookControlContext(
                node=order, allow_sync_webhooks=root.allow_sync_webhooks
            )

        try:
            # Orders that primary_key are not uuid are old int `id's`.
            # In migration `order_0128`, before migrating old `id's` to uuid,
            # old `id's` were saved to field `number`.
            order_pk = UUID(order_pk_or_number)
        except (AttributeError, ValueError):
            return (
                OrderByNumberLoader(info.context)
                .load(order_pk_or_number)
                .then(_wrap_with_sync_webhook_control_context)
            )

        return (
            OrderByIdLoader(info.context)
            .load(order_pk)
            .then(_wrap_with_sync_webhook_control_context)
        )

    @staticmethod
    def resolve_related(root: SyncWebhookControlContext[models.OrderEvent], info):
        event = root.node
        if not event.related_id:
            return None

        def _wrap_with_sync_webhook_control_context(event):
            if not event:
                return None
            return SyncWebhookControlContext(
                node=event, allow_sync_webhooks=root.allow_sync_webhooks
            )

        return (
            OrderEventsByIdLoader(info.context)
            .load(event.related_id)
            .then(_wrap_with_sync_webhook_control_context)
        )

    @staticmethod
    def resolve_discount(root: SyncWebhookControlContext[models.OrderEvent], info):
        discount_obj = root.node.parameters.get("discount")
        if not discount_obj:
            return None
        return get_order_discount_event(discount_obj)

    @staticmethod
    def resolve_status(root: SyncWebhookControlContext[models.OrderEvent], _info):
        return root.node.parameters.get("status")

    @staticmethod
    def resolve_reference(root: SyncWebhookControlContext[models.OrderEvent], _info):
        return root.node.parameters.get("reference")


class OrderEventCountableConnection(CountableConnection):
    class Meta:
        doc_category = DOC_CATEGORY_ORDERS
        node = OrderEvent


class FulfillmentLine(
    SyncWebhookControlContextModelObjectType[ModelObjectType[models.FulfillmentLine]]
):
    id = graphene.GlobalID(required=True, description="ID of the fulfillment line.")
    quantity = graphene.Int(
        required=True,
        description="The number of items included in the fulfillment line.",
    )
    order_line = graphene.Field(
        lambda: OrderLine,
        description="The order line to which the fulfillment line is related.",
    )

    class Meta:
        default_resolver = (
            SyncWebhookControlContextModelObjectType.resolver_with_context
        )
        description = "Represents line of the fulfillment."
        interfaces = [relay.Node]
        model = models.FulfillmentLine

    @staticmethod
    def resolve_order_line(
        root: SyncWebhookControlContext[models.FulfillmentLine], info
    ):
        def _wrap_with_sync_webhook_control_context(line):
            return SyncWebhookControlContext(
                node=line, allow_sync_webhooks=root.allow_sync_webhooks
            )

        return (
            OrderLineByIdLoader(info.context)
            .load(root.node.order_line_id)
            .then(_wrap_with_sync_webhook_control_context)
        )


class Fulfillment(
    SyncWebhookControlContextModelObjectType[ModelObjectType[models.Fulfillment]]
):
    id = graphene.GlobalID(required=True, description="ID of the fulfillment.")
    fulfillment_order = graphene.Int(
        required=True,
        description="Sequence in which the fulfillments were created for an order.",
    )
    status = FulfillmentStatusEnum(required=True, description="Status of fulfillment.")
    tracking_number = graphene.String(
        required=True, description="Fulfillment tracking number."
    )
    created = DateTime(
        required=True, description="Date and time when fulfillment was created."
    )
    lines = NonNullList(
        FulfillmentLine, description="List of lines for the fulfillment."
    )
    status_display = graphene.String(description="User-friendly fulfillment status.")
    warehouse = graphene.Field(
        Warehouse,
        required=False,
        description="Warehouse from fulfillment was fulfilled.",
    )
    shipping_refunded_amount = graphene.Field(
        Money,
        description="Amount of refunded shipping price.",
        required=False,
    )
    total_refunded_amount = graphene.Field(
        Money,
        description="Total refunded amount assigned to this fulfillment.",
        required=False,
    )

    class Meta:
        default_resolver = (
            SyncWebhookControlContextModelObjectType.resolver_with_context
        )
        description = "Represents order fulfillment."
        interfaces = [relay.Node, ObjectWithMetadata]
        model = models.Fulfillment

    @staticmethod
    def resolve_created(root: SyncWebhookControlContext[models.Fulfillment], _info):
        return root.node.created_at

    @staticmethod
    def resolve_lines(root: SyncWebhookControlContext[models.Fulfillment], info):
        def _wrap_with_sync_webhook_control_context(lines):
            return [
                SyncWebhookControlContext(
                    node=line, allow_sync_webhooks=root.allow_sync_webhooks
                )
                for line in lines
            ]

        return (
            FulfillmentLinesByFulfillmentIdLoader(info.context)
            .load(root.node.id)
            .then(_wrap_with_sync_webhook_control_context)
        )

    @staticmethod
    def resolve_status_display(
        root: SyncWebhookControlContext[models.Fulfillment], _info
    ):
        return root.node.get_status_display()

    @staticmethod
    def resolve_warehouse(root: SyncWebhookControlContext[models.Fulfillment], info):
        def _resolve_stock_warehouse(stock: Stock):
            return WarehouseByIdLoader(info.context).load(stock.warehouse_id)

        def _resolve_stock(fulfillment_lines: list[models.FulfillmentLine]):
            try:
                line = fulfillment_lines[0]
            except IndexError:
                return None

            if stock_id := line.stock_id:
                return (
                    StockByIdLoader(info.context)
                    .load(stock_id)
                    .then(_resolve_stock_warehouse)
                )
            return None

        return (
            FulfillmentLinesByFulfillmentIdLoader(info.context)
            .load(root.node.id)
            .then(_resolve_stock)
        )

    @staticmethod
    def resolve_shipping_refunded_amount(
        root: SyncWebhookControlContext[models.Fulfillment], info
    ):
        fulfillment = root.node
        if fulfillment.shipping_refund_amount is None:
            return None

        def _resolve_shipping_refund(order):
            return prices.Money(
                fulfillment.shipping_refund_amount, currency=order.currency
            )

        return (
            OrderByIdLoader(info.context)
            .load(fulfillment.order_id)
            .then(_resolve_shipping_refund)
        )

    @staticmethod
    def resolve_total_refunded_amount(
        root: SyncWebhookControlContext[models.Fulfillment], info
    ):
        fulfillment = root.node
        if fulfillment.total_refund_amount is None:
            return None

        def _resolve_total_refund_amount(order):
            return prices.Money(
                fulfillment.total_refund_amount, currency=order.currency
            )

        return (
            OrderByIdLoader(info.context)
            .load(fulfillment.order_id)
            .then(_resolve_total_refund_amount)
        )


class OrderLine(
    SyncWebhookControlContextModelObjectType[ModelObjectType[models.OrderLine]]
):
    id = graphene.GlobalID(required=True, description="ID of the order line.")
    product_name = graphene.String(
        required=True, description="Name of the product in order line."
    )
    variant_name = graphene.String(
        required=True, description="Name of the variant of product in order line."
    )
    product_sku = graphene.String(description="SKU of the product variant.")
    product_variant_id = graphene.String(description="The ID of the product variant.")
    is_shipping_required = graphene.Boolean(
        required=True, description="Whether the product variant requires shipping."
    )
    quantity = graphene.Int(
        required=True, description="Number of variant items ordered."
    )
    quantity_fulfilled = graphene.Int(
        required=True, description="Number of variant items fulfilled."
    )
    tax_rate = graphene.Float(
        required=True, description="Rate of tax applied on product variant."
    )
    digital_content_url = graphene.Field(DigitalContentUrl)
    thumbnail = ThumbnailField()
    unit_price = graphene.Field(
        TaxedMoney,
        description=(
            "Price of the single item in the order line with all the line-level "
            "discounts and order-level discount portions applied."
        ),
        required=True,
    )
    undiscounted_unit_price = graphene.Field(
        TaxedMoney,
        description=(
            "Price of the single item in the order line without any discount applied."
        ),
        required=True,
    )
    unit_discount = graphene.Field(
        Money,
        description=(
            "Sum of the line-level discounts applied to the order line. "
            "Order-level discounts which affect the line are not visible in this field."
            " For order-level discount portion (if any), please query `order.discounts`"
            " field."
        ),
        required=True,
    )
    unit_discount_reason = graphene.String(
        description=(
            "Reason for line-level discounts applied on the order line. Order-level "
            "discounts which affect the line are not visible in this field. For "
            "order-level discount reason (if any), please query `order.discounts` "
            "field."
        )
    )
    unit_discount_value = graphene.Field(
        PositiveDecimal,
        description=(
            "Value of the discount. Can store fixed value or percent value. "
            "This field shouldn't be used when multiple discounts affect the line. "
            "There is a limitation, that after running `checkoutComplete` mutation "
            "the field always stores fixed value."
        ),
        required=True,
    )
    unit_discount_type = graphene.Field(
        DiscountValueTypeEnum,
        description=(
            "Type of the discount: `fixed` or `percent`. This field shouldn't be used "
            "when multiple discounts affect the line. There is a limitation, that after"
            " running `checkoutComplete` mutation the field is always set to `fixed`."
        ),
    )
    total_price = graphene.Field(
        TaxedMoney, description="Price of the order line.", required=True
    )
    undiscounted_total_price = graphene.Field(
        TaxedMoney,
        description="Price of the order line without discounts.",
        required=True,
    )
    is_price_overridden = graphene.Boolean(
        description="Returns True, if the line unit price was overridden."
    )
    variant = graphene.Field(
        ProductVariant,
        required=False,
        description=(
            "A purchased product variant. Note: this field may be null if the variant "
            "has been removed from stock at all. Requires one of the following "
            "permissions to include the unpublished items: "
            f"{', '.join([p.name for p in ALL_PRODUCTS_PERMISSIONS])}."
        ),
    )
    translated_product_name = graphene.String(
        required=True, description="Product name in the customer's language"
    )
    translated_variant_name = graphene.String(
        required=True, description="Variant name in the customer's language"
    )
    allocations = PermissionsField(
        NonNullList(Allocation),
        description="List of allocations across warehouses.",
        permissions=[
            ProductPermissions.MANAGE_PRODUCTS,
            OrderPermissions.MANAGE_ORDERS,
        ],
    )
    sale_id = graphene.ID(
        required=False,
        description=(
            "Denormalized sale ID, set when order line is created for a product "
            "variant that is on sale."
        ),
    )
    quantity_to_fulfill = graphene.Int(
        required=True, description="A quantity of items remaining to be fulfilled."
    )
    tax_class = PermissionsField(
        TaxClass,
        description=("Denormalized tax class of the product in this order line."),
        required=False,
        permissions=[
            AuthorizationFilters.AUTHENTICATED_STAFF_USER,
            AuthorizationFilters.AUTHENTICATED_APP,
        ],
    )
    tax_class_name = graphene.Field(
        graphene.String,
        description="Denormalized name of the tax class.",
        required=False,
    )
    tax_class_metadata = NonNullList(
        MetadataItem,
        required=True,
        description="Denormalized public metadata of the tax class.",
    )
    tax_class_private_metadata = NonNullList(
        MetadataItem,
        required=True,
        description=(
            "Denormalized private metadata of the tax class. Requires staff "
            "permissions to access."
        ),
    )
    voucher_code = graphene.String(
        required=False, description="Voucher code that was used for this order line."
    )
    is_gift = graphene.Boolean(
        description="Determine if the line is a gift." + ADDED_IN_319 + PREVIEW_FEATURE,
    )
    discounts = NonNullList(
        "saleor.graphql.discount.types.discounts.OrderLineDiscount",
        description="List of applied discounts" + ADDED_IN_321,
    )

    class Meta:
        default_resolver = (
            SyncWebhookControlContextModelObjectType.resolver_with_context
        )
        description = "Represents order line of particular order."
        model = models.OrderLine
        interfaces = [relay.Node, ObjectWithMetadata]

    @staticmethod
    @traced_resolver
    def resolve_thumbnail(
        root: SyncWebhookControlContext[models.OrderLine],
        info,
        *,
        size: int = 256,
        format: str | None = None,
    ):
        variant_id = root.node.variant_id
        if not variant_id:
            return None

        format = get_thumbnail_format(format)
        size = get_thumbnail_size(size)

        def _get_image_from_media(image):
            def _resolve_url(thumbnail):
                url = get_image_or_proxy_url(
                    thumbnail, image.id, "ProductMedia", size, format
                )
                return Image(alt=image.alt, url=url)

            return (
                ThumbnailByProductMediaIdSizeAndFormatLoader(info.context)
                .load((image.id, size, format))
                .then(_resolve_url)
            )

        def _get_first_variant_image(
            all_medias: list[ProductMedia],
        ) -> ProductMedia | None:
            return next(
                (
                    media
                    for media in all_medias
                    if media.type == ProductMediaTypes.IMAGE
                ),
                None,
            )

        def _get_first_product_image(images):
            return _get_image_from_media(images[0]) if images else None

        def _resolve_thumbnail(result):
            product, variant_medias = result

            if image := _get_first_variant_image(variant_medias):
                return _get_image_from_media(image)

            # we failed to get image from variant, lets use first from product
            return (
                ImagesByProductIdLoader(info.context)
                .load(product.id)
                .then(_get_first_product_image)
            )

        variants_product = ProductByVariantIdLoader(info.context).load(variant_id)
        variant_medias = MediaByProductVariantIdLoader(info.context).load(variant_id)
        return Promise.all([variants_product, variant_medias]).then(_resolve_thumbnail)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_unit_price(root: SyncWebhookControlContext[models.OrderLine], info):
        order_line = root.node

        @allow_writer_in_context(info.context)
        def _resolve_unit_price(data):
            order, lines, manager = data
            database_connection_name = get_database_connection_name(info.context)
            return calculations.order_line_unit(
                order,
                order_line,
                manager,
                lines,
                database_connection_name=database_connection_name,
                allow_sync_webhooks=root.allow_sync_webhooks,
            ).price_with_discounts

        order = OrderByIdLoader(info.context).load(order_line.order_id)
        lines = OrderLinesByOrderIdLoader(info.context).load(order_line.order_id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([order, lines, manager]).then(_resolve_unit_price)

    @staticmethod
    def resolve_quantity_to_fulfill(
        root: SyncWebhookControlContext[models.OrderLine], info
    ):
        return root.node.quantity_unfulfilled

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_undiscounted_unit_price(
        root: SyncWebhookControlContext[models.OrderLine], info
    ):
        order_line = root.node

        @allow_writer_in_context(info.context)
        def _resolve_undiscounted_unit_price(data):
            order, lines, manager = data
            database_connection_name = get_database_connection_name(info.context)
            return calculations.order_line_unit(
                order,
                order_line,
                manager,
                lines,
                database_connection_name=database_connection_name,
                allow_sync_webhooks=root.allow_sync_webhooks,
            ).undiscounted_price

        order = OrderByIdLoader(info.context).load(order_line.order_id)
        lines = OrderLinesByOrderIdLoader(info.context).load(order_line.order_id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([order, lines, manager]).then(
            _resolve_undiscounted_unit_price
        )

    @staticmethod
    def resolve_unit_discount_type(
        root: SyncWebhookControlContext[models.OrderLine], info
    ):
        order_line = root.node

        def _resolve_unit_discount_type(data):
            order, lines, manager = data
            return calculations.order_line_unit_discount_type(
                order,
                order_line,
                manager,
                lines,
                allow_sync_webhooks=root.allow_sync_webhooks,
            )

        order = OrderByIdLoader(info.context).load(order_line.order_id)
        lines = OrderLinesByOrderIdLoader(info.context).load(order_line.order_id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([order, lines, manager]).then(_resolve_unit_discount_type)

    @staticmethod
    def resolve_unit_discount_value(
        root: SyncWebhookControlContext[models.OrderLine], info
    ):
        order_line = root.node

        def _resolve_unit_discount_value(data):
            order, lines, manager = data
            return calculations.order_line_unit_discount_value(
                order,
                order_line,
                manager,
                lines,
                allow_sync_webhooks=root.allow_sync_webhooks,
            )

        order = OrderByIdLoader(info.context).load(order_line.order_id)
        lines = OrderLinesByOrderIdLoader(info.context).load(order_line.order_id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([order, lines, manager]).then(_resolve_unit_discount_value)

    @staticmethod
    def resolve_unit_discount(root: SyncWebhookControlContext[models.OrderLine], info):
        order_line = root.node

        def _resolve_unit_discount(data):
            order, lines, manager = data
            return calculations.order_line_unit_discount(
                order,
                order_line,
                manager,
                lines,
                allow_sync_webhooks=root.allow_sync_webhooks,
            )

        order = OrderByIdLoader(info.context).load(order_line.order_id)
        lines = OrderLinesByOrderIdLoader(info.context).load(order_line.order_id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([order, lines, manager]).then(_resolve_unit_discount)

    @staticmethod
    @traced_resolver
    def resolve_tax_rate(root: SyncWebhookControlContext[models.OrderLine], info):
        order_line = root.node

        @allow_writer_in_context(info.context)
        def _resolve_tax_rate(data):
            order, lines, manager = data
            database_connection_name = get_database_connection_name(info.context)
            return calculations.order_line_tax_rate(
                order,
                order_line,
                manager,
                lines,
                database_connection_name=database_connection_name,
                allow_sync_webhooks=root.allow_sync_webhooks,
            ) or Decimal(0)

        order = OrderByIdLoader(info.context).load(order_line.order_id)
        lines = OrderLinesByOrderIdLoader(info.context).load(order_line.order_id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([order, lines, manager]).then(_resolve_tax_rate)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_total_price(root: SyncWebhookControlContext[models.OrderLine], info):
        order_line = root.node

        @allow_writer_in_context(info.context)
        def _resolve_total_price(data):
            order, lines, manager = data
            database_connection_name = get_database_connection_name(info.context)
            return calculations.order_line_total(
                order,
                order_line,
                manager,
                lines,
                database_connection_name=database_connection_name,
                allow_sync_webhooks=root.allow_sync_webhooks,
            ).price_with_discounts

        order = OrderByIdLoader(info.context).load(order_line.order_id)
        lines = OrderLinesByOrderIdLoader(info.context).load(order_line.order_id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([order, lines, manager]).then(_resolve_total_price)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_undiscounted_total_price(
        root: SyncWebhookControlContext[models.OrderLine], info
    ):
        order_line = root.node

        @allow_writer_in_context(info.context)
        def _resolve_undiscounted_total_price(data):
            order, lines, manager = data
            database_connection_name = get_database_connection_name(info.context)
            return calculations.order_line_total(
                order,
                order_line,
                manager,
                lines,
                database_connection_name=database_connection_name,
                allow_sync_webhooks=root.allow_sync_webhooks,
            ).undiscounted_price

        order = OrderByIdLoader(info.context).load(order_line.order_id)
        lines = OrderLinesByOrderIdLoader(info.context).load(order_line.order_id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([order, lines, manager]).then(
            _resolve_undiscounted_total_price
        )

    @staticmethod
    def resolve_translated_product_name(
        root: SyncWebhookControlContext[models.OrderLine], _info
    ):
        return root.node.translated_product_name

    @staticmethod
    def resolve_translated_variant_name(
        root: SyncWebhookControlContext[models.OrderLine], _info
    ):
        return root.node.translated_variant_name

    @staticmethod
    @traced_resolver
    def resolve_variant(root: SyncWebhookControlContext[models.OrderLine], info):
        context = info.context
        order_line = root.node
        if not order_line.variant_id:
            return None

        def requestor_has_access_to_variant(data):
            variant, channel = data

            requester = get_user_or_app_from_context(context)
            has_required_permission = has_one_of_permissions(
                requester, ALL_PRODUCTS_PERMISSIONS
            )
            if has_required_permission:
                return ChannelContext(node=variant, channel_slug=channel.slug)

            def product_is_available(product_channel_listing):
                if product_channel_listing and product_channel_listing.is_visible:
                    return ChannelContext(node=variant, channel_slug=channel.slug)
                return None

            return (
                ProductChannelListingByProductIdAndChannelSlugLoader(context)
                .load((variant.product_id, channel.slug))
                .then(product_is_available)
            )

        variant = ProductVariantByIdLoader(context).load(order_line.variant_id)
        channel = ChannelByOrderIdLoader(context).load(order_line.order_id)

        return Promise.all([variant, channel]).then(requestor_has_access_to_variant)

    @staticmethod
    def resolve_allocations(root: SyncWebhookControlContext[models.OrderLine], info):
        return AllocationsByOrderLineIdLoader(info.context).load(root.node.id)

    @staticmethod
    def resolve_tax_class(root: SyncWebhookControlContext[models.OrderLine], info):
        return (
            TaxClassByIdLoader(info.context).load(root.node.tax_class_id)
            if root.node.tax_class_id
            else None
        )

    @staticmethod
    def resolve_tax_class_metadata(
        root: SyncWebhookControlContext[models.OrderLine], _info
    ):
        return resolve_metadata(root.node.tax_class_metadata)

    @staticmethod
    def resolve_tax_class_private_metadata(
        root: SyncWebhookControlContext[models.OrderLine], info
    ):
        check_private_metadata_privilege(root.node, info)
        return resolve_metadata(root.node.tax_class_private_metadata)

    @staticmethod
    def resolve_discounts(root: SyncWebhookControlContext[models.OrderLine], info):
        line = root.node

        def with_manager_and_order(data):
            manager, order = data

            def handle_line_discount_from_checkout(data):
                channel, line_discounts = data

                # For legacy propagation, voucher discount was returned as OrderDiscount
                # when legacy is disabled, return the voucher discount as
                # OrderLineDiscount. It is a temporary solution to provide a grace
                # period for migration
                use_legacy = channel.use_legacy_line_discount_propagation_for_order
                if order.origin != OrderOrigin.CHECKOUT or not use_legacy:
                    return line_discounts

                discounts_to_return = []
                for discount in line_discounts:
                    # voucher discount propagated on the line is represented by
                    # OrderDiscount.
                    if discount.type == DiscountType.VOUCHER:
                        continue
                    discounts_to_return.append(discount)

                return discounts_to_return

            with allow_writer_in_context(info.context):
                fetch_order_prices_if_expired(
                    order, manager, allow_sync_webhooks=root.allow_sync_webhooks
                )
            channel_loader = ChannelByIdLoader(info.context).load(order.channel_id)
            order_line_discounts = OrderLineDiscountsByOrderLineIDLoader(
                info.context
            ).load(line.id)
            return Promise.all([channel_loader, order_line_discounts]).then(
                handle_line_discount_from_checkout
            )

        manager = get_plugin_manager_promise(info.context)
        order = OrderByIdLoader(info.context).load(line.order_id)
        return Promise.all([manager, order]).then(with_manager_and_order)


@federated_entity("id")
class Order(SyncWebhookControlContextModelObjectType[ModelObjectType[models.Order]]):
    id = graphene.GlobalID(required=True, description="ID of the order.")
    created = DateTime(
        required=True, description="Date and time when the order was created."
    )
    updated_at = DateTime(
        required=True, description="Date and time when the order was created."
    )
    status = OrderStatusEnum(required=True, description="Status of the order.")
    user = graphene.Field(
        User,
        description=(
            "User who placed the order. This field is set only for orders placed by "
            "authenticated users. Can be fetched for orders created in Saleor 3.2 "
            "and later, for other orders requires one of the following permissions: "
            f"{AccountPermissions.MANAGE_USERS.name}, "
            f"{OrderPermissions.MANAGE_ORDERS.name}, "
            f"{PaymentPermissions.HANDLE_PAYMENTS.name}, "
            f"{AuthorizationFilters.OWNER.name}."
        ),
    )
    tracking_client_id = graphene.String(
        required=True,
        description="Google Analytics tracking client ID. " + DEPRECATED_IN_3X_INPUT,
    )
    billing_address = graphene.Field(
        "saleor.graphql.account.types.Address",
        description=(
            "Billing address. The full data can be access for orders created "
            "in Saleor 3.2 and later, for other orders requires one of the following "
            f"permissions: {OrderPermissions.MANAGE_ORDERS.name}, "
            f"{AuthorizationFilters.OWNER.name}."
        ),
    )
    shipping_address = graphene.Field(
        "saleor.graphql.account.types.Address",
        description=(
            "Shipping address. The full data can be access for orders created "
            "in Saleor 3.2 and later, for other orders requires one of the following "
            f"permissions: {OrderPermissions.MANAGE_ORDERS.name}, "
            f"{AuthorizationFilters.OWNER.name}."
        ),
    )
    shipping_method_name = graphene.String(description="Method used for shipping.")
    collection_point_name = graphene.String(
        description="Name of the collection point where the order should be picked up by the customer."
    )
    channel = graphene.Field(
        Channel,
        required=True,
        description="Channel through which the order was placed.",
    )
    fulfillments = NonNullList(
        Fulfillment, required=True, description="List of shipments for the order."
    )
    lines = NonNullList(
        lambda: OrderLine, required=True, description="List of order lines."
    )
    actions = NonNullList(
        OrderAction,
        description=(
            "List of actions that can be performed in the current state of an order."
        ),
        required=True,
    )
    available_shipping_methods = NonNullList(
        ShippingMethod,
        description="Shipping methods that can be used with this order.",
        required=False,
        deprecation_reason="Use `shippingMethods`, this field will be removed in 4.0",
    )
    shipping_methods = NonNullList(
        ShippingMethod,
        description="Shipping methods related to this order.",
        required=True,
    )
    available_collection_points = NonNullList(
        Warehouse,
        description=("Collection points that can be used for this order."),
        required=True,
    )
    invoices = NonNullList(
        Invoice,
        description=(
            "List of order invoices. Can be fetched for orders created in Saleor 3.2 "
            "and later, for other orders requires one of the following permissions: "
            f"{OrderPermissions.MANAGE_ORDERS.name}, {AuthorizationFilters.OWNER.name}."
        ),
        required=True,
    )
    number = graphene.String(
        description="User-friendly number of an order.", required=True
    )
    original = graphene.ID(
        description="The ID of the order that was the base for this order."
    )
    origin = OrderOriginEnum(description="The order origin.", required=True)
    is_paid = graphene.Boolean(
        description="Informs if an order is fully paid.", required=True
    )
    payment_status = PaymentChargeStatusEnum(
        description="Internal payment status.", required=True
    )
    payment_status_display = graphene.String(
        description="User-friendly payment status.", required=True
    )
    authorize_status = OrderAuthorizeStatusEnum(
        description=("The authorize status of the order."),
        required=True,
    )
    charge_status = OrderChargeStatusEnum(
        description=("The charge status of the order."),
        required=True,
    )
    tax_exemption = graphene.Boolean(
        description=("Returns True if order has to be exempt from taxes."),
        required=True,
    )
    transactions = NonNullList(
        TransactionItem,
        description=(
            "List of transactions for the order. Requires one of the "
            "following permissions: MANAGE_ORDERS, HANDLE_PAYMENTS."
        ),
        required=True,
    )
    payments = NonNullList(
        Payment, description="List of payments for the order.", required=True
    )
    total = graphene.Field(
        TaxedMoney, description="Total amount of the order.", required=True
    )
    undiscounted_total = graphene.Field(
        TaxedMoney, description="Undiscounted total amount of the order.", required=True
    )
    shipping_method = graphene.Field(
        ShippingMethod,
        description="Shipping method for this order.",
        deprecation_reason="Use `deliveryMethod` instead.",
    )
    undiscounted_shipping_price = graphene.Field(
        Money,
        description="Undiscounted total price of shipping." + ADDED_IN_319,
        required=True,
    )
    shipping_price = graphene.Field(
        TaxedMoney, description="Total price of shipping.", required=True
    )
    shipping_tax_rate = graphene.Float(
        required=True, description="The shipping tax rate value."
    )
    shipping_tax_class = PermissionsField(
        TaxClass,
        description="Denormalized tax class assigned to the shipping method.",
        required=False,
        permissions=[
            AuthorizationFilters.AUTHENTICATED_STAFF_USER,
            AuthorizationFilters.AUTHENTICATED_APP,
        ],
    )
    shipping_tax_class_name = graphene.Field(
        graphene.String,
        description=(
            "Denormalized name of the tax class assigned to the shipping method."
        ),
        required=False,
    )
    shipping_tax_class_metadata = NonNullList(
        MetadataItem,
        required=True,
        description=(
            "Denormalized public metadata of the shipping method's tax class."
        ),
    )
    shipping_tax_class_private_metadata = NonNullList(
        MetadataItem,
        required=True,
        description=(
            "Denormalized private metadata of the shipping method's tax class. "
            "Requires staff permissions to access."
        ),
    )
    token = graphene.String(
        required=True,
        deprecation_reason="Use `id` instead.",
    )
    voucher = graphene.Field(Voucher, description="Voucher linked to the order.")
    voucher_code = graphene.String(
        required=False,
        description="Voucher code that was used for Order." + ADDED_IN_318,
    )
    gift_cards = NonNullList(
        GiftCard, description="List of user gift cards.", required=True
    )
    customer_note = graphene.String(
        required=True,
        description="Additional information provided by the customer about the order.",
    )
    weight = graphene.Field(Weight, required=True, description="Weight of the order.")
    redirect_url = graphene.String(
        description="URL to which user should be redirected after order is placed."
    )
    subtotal = graphene.Field(
        TaxedMoney,
        description="The sum of line prices not including shipping.",
        required=True,
    )
    status_display = graphene.String(
        description="User-friendly order status.", required=True
    )
    can_finalize = graphene.Boolean(
        description=(
            "Informs whether a draft order can be finalized"
            "(turned into a regular order)."
        ),
        required=True,
    )
    total_authorized = graphene.Field(
        Money, description="Amount authorized for the order.", required=True
    )
    total_captured = graphene.Field(
        Money,
        description="Amount captured for the order. ",
        deprecation_reason="Use `totalCharged` instead.",
        required=True,
    )
    total_charged = graphene.Field(
        Money, description="Amount charged for the order.", required=True
    )

    total_canceled = graphene.Field(
        Money,
        description="Amount canceled for the order.",
        required=True,
    )

    events = PermissionsField(
        NonNullList(OrderEvent),
        description="List of events associated with the order.",
        permissions=[OrderPermissions.MANAGE_ORDERS],
        required=True,
    )
    total_balance = graphene.Field(
        Money,
        description="The difference between the paid and the order total amount.",
        required=True,
    )
    user_email = graphene.String(
        description=(
            "Email address of the customer. The full data can be access for orders "
            "created in Saleor 3.2 and later, for other orders requires one of "
            f"the following permissions: {OrderPermissions.MANAGE_ORDERS.name}, "
            f"{AuthorizationFilters.OWNER.name}."
        ),
        required=False,
    )
    is_shipping_required = graphene.Boolean(
        description="Returns True, if order requires shipping.", required=True
    )
    delivery_method = graphene.Field(
        DeliveryMethod,
        description=("The delivery method selected for this order."),
    )
    language_code = graphene.String(
        deprecation_reason="Use the `languageCodeEnum` field to fetch the language code.",
        required=True,
    )
    language_code_enum = graphene.Field(
        LanguageCodeEnum, description="Order language code.", required=True
    )
    discount = graphene.Field(
        Money,
        description="Returns applied discount.",
        deprecation_reason="Use the `discounts` field instead.",
    )
    discount_name = graphene.String(
        description="Discount name.",
        deprecation_reason="Use the `discounts` field instead.",
    )
    translated_discount_name = graphene.String(
        description="Translated discount name.",
        deprecation_reason="Use the `discounts` field instead.",
    )
    discounts = NonNullList(
        "saleor.graphql.discount.types.OrderDiscount",
        description="List of all discounts assigned to the order.",
        required=True,
    )
    errors = NonNullList(
        OrderError,
        description="List of errors that occurred during order validation.",
        default_value=[],
        required=True,
    )
    display_gross_prices = graphene.Boolean(
        description=("Determines whether displayed prices should include taxes."),
        required=True,
    )
    external_reference = graphene.String(
        description="External ID of this order.", required=False
    )
    checkout_id = graphene.ID(
        description=("ID of the checkout that the order was created from."),
        required=False,
    )

    granted_refunds = PermissionsField(
        NonNullList(OrderGrantedRefund),
        required=True,
        description="List of granted refunds.",
        permissions=[OrderPermissions.MANAGE_ORDERS],
    )
    total_granted_refund = PermissionsField(
        Money,
        required=True,
        description="Total amount of granted refund.",
        permissions=[OrderPermissions.MANAGE_ORDERS],
    )
    total_refunded = graphene.Field(
        Money, required=True, description="Total refund amount for the order."
    )
    total_refund_pending = PermissionsField(
        Money,
        required=True,
        description=(
            "Total amount of ongoing refund requests for the order's transactions."
        ),
        permissions=[OrderPermissions.MANAGE_ORDERS],
    )
    total_authorize_pending = PermissionsField(
        Money,
        required=True,
        description=(
            "Total amount of ongoing authorize requests for the order's transactions."
        ),
        permissions=[OrderPermissions.MANAGE_ORDERS],
    )
    total_charge_pending = PermissionsField(
        Money,
        required=True,
        description=(
            "Total amount of ongoing charge requests for the order's transactions."
        ),
        permissions=[OrderPermissions.MANAGE_ORDERS],
    )
    total_cancel_pending = PermissionsField(
        Money,
        required=True,
        description=(
            "Total amount of ongoing cancel requests for the order's transactions."
        ),
        permissions=[OrderPermissions.MANAGE_ORDERS],
    )

    total_remaining_grant = PermissionsField(
        Money,
        required=True,
        description=(
            "The difference amount between granted refund and the "
            "amounts that are pending and refunded."
        ),
        permissions=[OrderPermissions.MANAGE_ORDERS],
    )

    class Meta:
        default_resolver = (
            SyncWebhookControlContextModelObjectType.resolver_with_context
        )
        description = "Represents an order in the shop."
        interfaces = [relay.Node, ObjectWithMetadata]
        model = models.Order

    @staticmethod
    def resolve_created(root: SyncWebhookControlContext[models.Order], _info):
        return root.node.created_at

    @staticmethod
    def resolve_channel(root: SyncWebhookControlContext[models.Order], info):
        return ChannelByIdLoader(info.context).load(root.node.channel_id)

    @staticmethod
    def resolve_token(root: SyncWebhookControlContext[models.Order], info):
        return root.node.id

    @staticmethod
    @prevent_sync_event_circular_query
    def resolve_discounts(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        # Line-lvl voucher discounts are represented as OrderDiscount objects for order
        # created from checkout.
        def wrap_line_discounts_from_checkout(data):
            channel, order_discounts = data

            if order.origin != OrderOrigin.CHECKOUT:
                return order_discounts

            # voucher discount is stored as OrderLineDiscount object in DB.
            # for backward compatibility, when legacy propagation is enabled
            # we convert the order-line-discounts into single OrderDiscount
            # It is a temporary solution to provide a grace period for migration
            if not channel.use_legacy_line_discount_propagation_for_order:
                return order_discounts

            def wrap_order_line(order_lines):
                def wrap_order_line_discount(
                    order_line_discounts: list[list[discount_models.OrderLineDiscount]],
                ):
                    # This affects orders created from checkout and applies
                    # specifically to vouchers of the types: `SPECIFIC_PRODUCT` and
                    # `ENTIRE_ORDER` with `applyOncePerOrder` enabled.
                    # discounts from these vouchers should be represented as
                    # OrderDiscount, but they are stored as OrderLineDiscount in
                    # database. To not add any breaking change, we create artifical
                    # order discount object
                    artificial_order_discount = None
                    for line_discount_list in order_line_discounts:
                        for line_discount in line_discount_list:
                            if line_discount.type != DiscountType.VOUCHER:
                                continue

                            if artificial_order_discount is None:
                                artificial_order_discount = (
                                    discount_models.OrderDiscount(
                                        id=line_discount.id,
                                        name=line_discount.name,
                                        type=line_discount.type,
                                        value_type=line_discount.value_type,
                                        value=line_discount.value,
                                        amount_value=line_discount.amount_value,
                                        currency=line_discount.currency,
                                        reason=line_discount.reason,
                                        translated_name=line_discount.translated_name,
                                    )
                                )
                            else:
                                artificial_order_discount.amount_value += (
                                    line_discount.amount_value
                                )

                    if artificial_order_discount:
                        return order_discounts + [artificial_order_discount]
                    return order_discounts

                return (
                    OrderLineDiscountsByOrderLineIDLoader(info.context)
                    .load_many([line.pk for line in order_lines])
                    .then(wrap_order_line_discount)
                )

            return (
                OrderLinesByOrderIdLoader(info.context)
                .load(order.id)
                .then(wrap_order_line)
            )

        def with_manager(manager):
            with allow_writer_in_context(info.context):
                fetch_order_prices_if_expired(
                    order, manager, allow_sync_webhooks=root.allow_sync_webhooks
                )
            channel_loader = ChannelByIdLoader(info.context).load(order.channel_id)
            order_discounts = OrderDiscountsByOrderIDLoader(info.context).load(order.id)
            return Promise.all([channel_loader, order_discounts]).then(
                wrap_line_discounts_from_checkout
            )

        return get_plugin_manager_promise(info.context).then(with_manager)

    @staticmethod
    @traced_resolver
    def resolve_discount(root: SyncWebhookControlContext[models.Order], info):
        def return_voucher_discount(discounts) -> Money | None:
            if not discounts:
                return None
            for discount in discounts:
                if discount.type == DiscountType.VOUCHER:
                    return Money(
                        amount=discount.amount_value, currency=discount.currency
                    )
            return None

        return (
            OrderDiscountsByOrderIDLoader(info.context)
            .load(root.node.id)
            .then(return_voucher_discount)
        )

    @staticmethod
    @traced_resolver
    def resolve_discount_name(root: SyncWebhookControlContext[models.Order], info):
        def return_voucher_name(discounts) -> Money | None:
            if not discounts:
                return None
            for discount in discounts:
                if discount.type == DiscountType.VOUCHER:
                    return discount.name
            return None

        return (
            OrderDiscountsByOrderIDLoader(info.context)
            .load(root.node.id)
            .then(return_voucher_name)
        )

    @staticmethod
    @traced_resolver
    def resolve_translated_discount_name(
        root: SyncWebhookControlContext[models.Order], info
    ):
        def return_voucher_translated_name(discounts) -> Money | None:
            if not discounts:
                return None
            for discount in discounts:
                if discount.type == DiscountType.VOUCHER:
                    return discount.translated_name
            return None

        return (
            OrderDiscountsByOrderIDLoader(info.context)
            .load(root.node.id)
            .then(return_voucher_translated_name)
        )

    @staticmethod
    @traced_resolver
    def resolve_billing_address(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        def _resolve_billing_address(data):
            if isinstance(data, Address):
                user = None
                address = data
            else:
                user, address = data

            requester = get_user_or_app_from_context(info.context)
            if order.use_old_id is False or is_owner_or_has_one_of_perms(
                requester, user, OrderPermissions.MANAGE_ORDERS
            ):
                return address
            return obfuscate_address(address)

        if not order.billing_address_id:
            return None

        if order.user_id:
            user = UserByUserIdLoader(info.context).load(order.user_id)
            address = AddressByIdLoader(info.context).load(order.billing_address_id)
            return Promise.all([user, address]).then(_resolve_billing_address)
        return (
            AddressByIdLoader(info.context)
            .load(order.billing_address_id)
            .then(_resolve_billing_address)
        )

    @staticmethod
    @traced_resolver
    def resolve_shipping_address(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        def _resolve_shipping_address(data):
            if isinstance(data, Address):
                user = None
                address = data
            else:
                user, address = data
            requester = get_user_or_app_from_context(info.context)
            if order.use_old_id is False or is_owner_or_has_one_of_perms(
                requester, user, OrderPermissions.MANAGE_ORDERS
            ):
                return address
            return obfuscate_address(address)

        if not order.shipping_address_id:
            return None

        if order.user_id:
            user = UserByUserIdLoader(info.context).load(order.user_id)
            address = AddressByIdLoader(info.context).load(order.shipping_address_id)
            return Promise.all([user, address]).then(_resolve_shipping_address)
        return (
            AddressByIdLoader(info.context)
            .load(order.shipping_address_id)
            .then(_resolve_shipping_address)
        )

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_undiscounted_shipping_price(
        root: SyncWebhookControlContext[models.Order], info
    ):
        order = root.node

        def _resolve_undiscounted_shipping_price(data):
            lines, manager = data
            database_connection_name = get_database_connection_name(info.context)
            return calculations.order_undiscounted_shipping(
                order,
                manager,
                lines,
                database_connection_name=database_connection_name,
                allow_sync_webhooks=root.allow_sync_webhooks,
            )

        lines = OrderLinesByOrderIdLoader(info.context).load(order.id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([lines, manager]).then(_resolve_undiscounted_shipping_price)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_shipping_price(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        @allow_writer_in_context(info.context)
        def _resolve_shipping_price(data):
            lines, manager = data
            database_connection_name = get_database_connection_name(info.context)
            return calculations.order_shipping(
                order,
                manager,
                lines,
                database_connection_name=database_connection_name,
                allow_sync_webhooks=root.allow_sync_webhooks,
            )

        lines = OrderLinesByOrderIdLoader(info.context).load(order.id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([lines, manager]).then(_resolve_shipping_price)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_shipping_tax_rate(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        @allow_writer_in_context(info.context)
        def _resolve_shipping_tax_rate(data):
            lines, manager = data
            database_connection_name = get_database_connection_name(info.context)
            return calculations.order_shipping_tax_rate(
                order,
                manager,
                lines,
                database_connection_name=database_connection_name,
                allow_sync_webhooks=root.allow_sync_webhooks,
            ) or Decimal(0)

        lines = OrderLinesByOrderIdLoader(info.context).load(order.id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([lines, manager]).then(_resolve_shipping_tax_rate)

    @staticmethod
    def resolve_actions(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        def _resolve_actions(payments):
            actions = []
            payment = get_last_payment(payments)
            if order.can_capture(payment):
                actions.append(OrderAction.CAPTURE)
            if order.can_mark_as_paid(payments):
                actions.append(OrderAction.MARK_AS_PAID)
            if order.can_refund(payment):
                actions.append(OrderAction.REFUND)
            if order.can_void(payment):
                actions.append(OrderAction.VOID)
            return actions

        return (
            PaymentsByOrderIdLoader(info.context).load(order.id).then(_resolve_actions)
        )

    @staticmethod
    @traced_resolver
    def resolve_subtotal(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        @allow_writer_in_context(info.context)
        def _resolve_subtotal(data):
            order_lines, manager = data
            database_connection_name = get_database_connection_name(info.context)
            return calculations.order_subtotal(
                order,
                manager,
                order_lines,
                database_connection_name=database_connection_name,
                allow_sync_webhooks=root.allow_sync_webhooks,
            )

        order_lines = OrderLinesByOrderIdLoader(info.context).load(order.id)
        manager = get_plugin_manager_promise(info.context)

        return Promise.all([order_lines, manager]).then(_resolve_subtotal)

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    @plugin_manager_promise_callback
    def resolve_total(root: SyncWebhookControlContext[models.Order], info, manager):
        order = root.node

        @allow_writer_in_context(info.context)
        def _resolve_total(lines):
            database_connection_name = get_database_connection_name(info.context)
            return calculations.order_total(
                order,
                manager,
                lines,
                database_connection_name=database_connection_name,
                allow_sync_webhooks=root.allow_sync_webhooks,
            )

        return (
            OrderLinesByOrderIdLoader(info.context).load(order.id).then(_resolve_total)
        )

    @staticmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    def resolve_undiscounted_total(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        @allow_writer_in_context(info.context)
        def _resolve_undiscounted_total(lines_and_manager):
            lines, manager = lines_and_manager
            database_connection_name = get_database_connection_name(info.context)
            return calculations.order_undiscounted_total(
                order,
                manager,
                lines,
                database_connection_name=database_connection_name,
                allow_sync_webhooks=root.allow_sync_webhooks,
            )

        lines = OrderLinesByOrderIdLoader(info.context).load(order.id)
        manager = get_plugin_manager_promise(info.context)
        return Promise.all([lines, manager]).then(_resolve_undiscounted_total)

    @staticmethod
    def resolve_total_authorized(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        def _resolve_total_get_total_authorized(data):
            transactions, payments = data
            if transactions:
                authorized_money = prices.Money(Decimal(0), order.currency)
                for transaction in transactions:
                    authorized_money += transaction.amount_authorized
                return quantize_price(authorized_money, order.currency)
            return get_total_authorized(payments, order.currency)

        transactions = TransactionItemsByOrderIDLoader(info.context).load(order.id)
        payments = PaymentsByOrderIdLoader(info.context).load(order.id)
        return Promise.all([transactions, payments]).then(
            _resolve_total_get_total_authorized
        )

    @staticmethod
    def resolve_total_canceled(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        def _resolve_total_canceled(transactions):
            canceled_money = prices.Money(Decimal(0), order.currency)
            if transactions:
                for transaction in transactions:
                    canceled_money += transaction.amount_canceled
            return quantize_price(canceled_money, order.currency)

        return (
            TransactionItemsByOrderIDLoader(info.context)
            .load(order.id)
            .then(_resolve_total_canceled)
        )

    @staticmethod
    def resolve_total_captured(root: SyncWebhookControlContext[models.Order], info):
        return root.node.total_charged

    @staticmethod
    def resolve_total_charged(root: SyncWebhookControlContext[models.Order], info):
        return root.node.total_charged

    @staticmethod
    def resolve_total_balance(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        def _resolve_total_balance(data):
            granted_refunds, transactions, payments = data
            if any(p.is_active for p in payments):
                return order.total_balance

            total_granted_refund = sum(
                [granted_refund.amount for granted_refund in granted_refunds],
                zero_money(order.currency),
            )
            total_charged = prices.Money(Decimal(0), order.currency)

            for transaction in transactions:
                total_charged += transaction.amount_charged
                total_charged += transaction.amount_charge_pending
            order_granted_refunds_difference = order.total.gross - total_granted_refund
            return total_charged - order_granted_refunds_difference

        granted_refunds = OrderGrantedRefundsByOrderIdLoader(info.context).load(
            order.id
        )
        transactions = TransactionItemsByOrderIDLoader(info.context).load(order.id)
        payments = PaymentsByOrderIdLoader(info.context).load(order.id)
        return Promise.all([granted_refunds, transactions, payments]).then(
            _resolve_total_balance
        )

    @staticmethod
    def resolve_fulfillments(root: SyncWebhookControlContext[models.Order], info):
        def _resolve_fulfillments(fulfillments):
            return_all_fulfillments = is_staff_user(info.context) or is_app(
                info.context
            )

            if return_all_fulfillments:
                fulfillments_to_return = fulfillments
            else:
                fulfillments_to_return = filter(
                    lambda fulfillment: fulfillment.status
                    != FulfillmentStatus.CANCELED,
                    fulfillments,
                )
            return [
                SyncWebhookControlContext(
                    node=fulfillment, allow_sync_webhooks=root.allow_sync_webhooks
                )
                for fulfillment in fulfillments_to_return
            ]

        return (
            FulfillmentsByOrderIdLoader(info.context)
            .load(root.node.id)
            .then(_resolve_fulfillments)
        )

    @staticmethod
    def resolve_lines(root: SyncWebhookControlContext[models.Order], info):
        def _wrap_with_sync_webhook_control_context(lines):
            return [
                SyncWebhookControlContext(
                    node=line, allow_sync_webhooks=root.allow_sync_webhooks
                )
                for line in lines
            ]

        return (
            OrderLinesByOrderIdLoader(info.context)
            .load(root.node.id)
            .then(_wrap_with_sync_webhook_control_context)
        )

    @staticmethod
    def resolve_events(root: SyncWebhookControlContext[models.Order], _info):
        def _wrap_with_sync_webhook_control_context(events):
            return [
                SyncWebhookControlContext(
                    node=event, allow_sync_webhooks=root.allow_sync_webhooks
                )
                for event in events
            ]

        return (
            OrderEventsByOrderIdLoader(_info.context)
            .load(root.node.id)
            .then(_wrap_with_sync_webhook_control_context)
        )

    @staticmethod
    def resolve_is_paid(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        def _resolve_is_paid(transactions):
            if transactions:
                charged_money = prices.Money(Decimal(0), order.currency)
                for transaction in transactions:
                    charged_money += transaction.amount_charged
                return charged_money >= order.total.gross
            return order.is_fully_paid()

        return (
            TransactionItemsByOrderIDLoader(info.context)
            .load(order.id)
            .then(_resolve_is_paid)
        )

    @staticmethod
    def resolve_number(root: SyncWebhookControlContext[models.Order], _info):
        return str(root.node.number)

    @staticmethod
    @traced_resolver
    def resolve_payment_status(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        def _resolve_payment_status(data):
            transactions, payments, fulfillments, granted_refunds = data

            total_fulfillment_refund = sum(
                [
                    fulfillment.total_refund_amount
                    for fulfillment in fulfillments
                    if fulfillment.total_refund_amount
                ]
            )
            if (
                total_fulfillment_refund != 0
                and total_fulfillment_refund == order.total.gross.amount
            ):
                return ChargeStatus.FULLY_REFUNDED

            if transactions:
                return get_payment_status_for_order(order, granted_refunds)
            last_payment = get_last_payment(payments)
            if not last_payment:
                if order.total.gross.amount == 0:
                    return ChargeStatus.FULLY_CHARGED
                return ChargeStatus.NOT_CHARGED
            return last_payment.charge_status

        transactions = TransactionItemsByOrderIDLoader(info.context).load(order.id)
        payments = PaymentsByOrderIdLoader(info.context).load(order.id)
        fulfillments = FulfillmentsByOrderIdLoader(info.context).load(order.id)
        granted_refunds = OrderGrantedRefundsByOrderIdLoader(info.context).load(
            order.id
        )
        return Promise.all(
            [transactions, payments, fulfillments, granted_refunds]
        ).then(_resolve_payment_status)

    @staticmethod
    def resolve_payment_status_display(
        root: SyncWebhookControlContext[models.Order], info
    ):
        order = root.node

        def _resolve_payment_status(data):
            transactions, payments, granted_refunds = data
            if transactions:
                status = get_payment_status_for_order(order, granted_refunds)
                return dict(ChargeStatus.CHOICES).get(status)
            last_payment = get_last_payment(payments)
            if not last_payment:
                if order.total.gross.amount == 0:
                    return dict(ChargeStatus.CHOICES).get(ChargeStatus.FULLY_CHARGED)
                return dict(ChargeStatus.CHOICES).get(ChargeStatus.NOT_CHARGED)
            return last_payment.get_charge_status_display()

        transactions = TransactionItemsByOrderIDLoader(info.context).load(order.id)
        payments = PaymentsByOrderIdLoader(info.context).load(order.id)
        granted_refunds = OrderGrantedRefundsByOrderIdLoader(info.context).load(
            order.id
        )
        return Promise.all([transactions, payments, granted_refunds]).then(
            _resolve_payment_status
        )

    @staticmethod
    def resolve_payments(root: SyncWebhookControlContext[models.Order], info):
        return PaymentsByOrderIdLoader(info.context).load(root.node.id)

    @staticmethod
    @one_of_permissions_required(
        [OrderPermissions.MANAGE_ORDERS, PaymentPermissions.HANDLE_PAYMENTS]
    )
    def resolve_transactions(root: SyncWebhookControlContext[models.Order], info):
        return TransactionItemsByOrderIDLoader(info.context).load(root.node.id)

    @staticmethod
    def resolve_status_display(root: SyncWebhookControlContext[models.Order], _info):
        return root.node.get_status_display()

    @staticmethod
    @traced_resolver
    def resolve_can_finalize(root: SyncWebhookControlContext[models.Order], info):
        order = root.node
        if order.status == OrderStatus.DRAFT:

            @allow_writer_in_context(info.context)
            def _validate_draft_order(data):
                lines, manager = data
                country = get_order_country(order)
                database_connection_name = get_database_connection_name(info.context)
                try:
                    validate_draft_order(
                        order=order,
                        lines=lines,
                        country=country,
                        manager=manager,
                        database_connection_name=database_connection_name,
                        allow_sync_webhooks=root.allow_sync_webhooks,
                    )
                except ValidationError:
                    return False
                return True

            lines = OrderLinesByOrderIdLoader(info.context).load(order.id)
            manager = get_plugin_manager_promise(info.context)
            return Promise.all([lines, manager]).then(_validate_draft_order)
        return True

    @staticmethod
    def resolve_user_email(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        def _resolve_user_email(user):
            requester = get_user_or_app_from_context(info.context)
            email_to_return = None
            if order.user_email:
                email_to_return = order.user_email
            elif user:
                email_to_return = user.email

            if order.use_old_id is False or is_owner_or_has_one_of_perms(
                requester, user, OrderPermissions.MANAGE_ORDERS
            ):
                return email_to_return
            return obfuscate_email(email_to_return)

        if not order.user_id:
            return _resolve_user_email(None)

        return (
            UserByUserIdLoader(info.context)
            .load(order.user_id)
            .then(_resolve_user_email)
        )

    @staticmethod
    def resolve_user(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        def _resolve_user(user):
            requester = get_user_or_app_from_context(info.context)
            check_is_owner_or_has_one_of_perms(
                requester,
                user,
                AccountPermissions.MANAGE_USERS,
                OrderPermissions.MANAGE_ORDERS,
                PaymentPermissions.HANDLE_PAYMENTS,
            )
            return user

        if not order.user_id:
            return None

        return UserByUserIdLoader(info.context).load(order.user_id).then(_resolve_user)

    @staticmethod
    def resolve_shipping_method(root: SyncWebhookControlContext[models.Order], info):
        order = root.node
        external_app_shipping_id = get_external_shipping_id(order)

        if external_app_shipping_id:
            tax_config = TaxConfigurationByChannelId(info.context).load(
                order.channel_id
            )

            def with_tax_config(tax_config):
                prices_entered_with_tax = tax_config.prices_entered_with_tax
                price = (
                    order.shipping_price_gross
                    if prices_entered_with_tax
                    else order.shipping_price_net
                )
                return ShippingMethodData(
                    id=external_app_shipping_id,
                    name=order.shipping_method_name,
                    price=price,
                )

            return tax_config.then(with_tax_config)

        if not order.shipping_method_id:
            return None

        def wrap_shipping_method_with_channel_context(data):
            shipping_method, channel = data
            listing = (
                ShippingMethodChannelListingByShippingMethodIdAndChannelSlugLoader(
                    info.context
                ).load((shipping_method.id, channel.slug))
            )

            tax_class = None
            if shipping_method.tax_class_id:
                tax_class = TaxClassByIdLoader(info.context).load(
                    shipping_method.tax_class_id
                )

            def calculate_price(data) -> ShippingMethodData | None:
                listing, tax_class = data
                if not listing:
                    return None
                return convert_to_shipping_method_data(
                    shipping_method, listing, tax_class
                )

            return Promise.all([listing, tax_class]).then(calculate_price)

        shipping_method = ShippingMethodByIdLoader(info.context).load(
            int(order.shipping_method_id)
        )
        channel = ChannelByIdLoader(info.context).load(order.channel_id)

        return Promise.all([shipping_method, channel]).then(
            wrap_shipping_method_with_channel_context
        )

    @classmethod
    def resolve_delivery_method(
        cls, root: SyncWebhookControlContext[models.Order], info
    ):
        order = root.node
        if order.shipping_method_id or get_external_shipping_id(order):
            return cls.resolve_shipping_method(root, info)
        if order.collection_point_id:
            collection_point = WarehouseByIdLoader(info.context).load(
                order.collection_point_id
            )
            return collection_point
        return None

    @classmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    # TODO: We should optimize it in/after PR#5819
    def resolve_shipping_methods(
        cls, root: SyncWebhookControlContext[models.Order], info
    ):
        order = root.node

        def with_channel(data):
            channel, manager = data
            database_connection_name = get_database_connection_name(info.context)

            @allow_writer_in_context(info.context)
            def with_listings(channel_listings):
                return get_valid_shipping_methods_for_order(
                    order,
                    channel_listings,
                    manager,
                    database_connection_name=database_connection_name,
                    allow_sync_webhooks=root.allow_sync_webhooks,
                )

            return (
                ShippingMethodChannelListingByChannelSlugLoader(info.context)
                .load(channel.slug)
                .then(with_listings)
            )

        channel = ChannelByIdLoader(info.context).load(order.channel_id)
        manager = get_plugin_manager_promise(info.context)

        return Promise.all([channel, manager]).then(with_channel)

    @classmethod
    @traced_resolver
    @prevent_sync_event_circular_query
    # TODO: We should optimize it in/after PR#5819
    def resolve_available_shipping_methods(
        cls, root: SyncWebhookControlContext[models.Order], info
    ):
        return cls.resolve_shipping_methods(root, info).then(
            lambda methods: [method for method in methods if method.active]
        )

    @classmethod
    @traced_resolver
    def resolve_available_collection_points(
        cls, root: SyncWebhookControlContext[models.Order], info
    ):
        order = root.node

        def get_available_collection_points(
            wrapped_lines: list[SyncWebhookControlContext[models.OrderLine]],
        ):
            database_connection_name = get_database_connection_name(info.context)
            lines = [line.node for line in wrapped_lines]
            return get_valid_collection_points_for_order(
                lines, order.channel_id, database_connection_name
            )

        return cls.resolve_lines(root, info).then(get_available_collection_points)

    @staticmethod
    def resolve_invoices(root: SyncWebhookControlContext[models.Order], info):
        order = root.node
        requester = get_user_or_app_from_context(info.context)
        if order.use_old_id is True:
            check_is_owner_or_has_one_of_perms(
                requester, order.user, OrderPermissions.MANAGE_ORDERS
            )
        return InvoicesByOrderIdLoader(info.context).load(order.id)

    @staticmethod
    def resolve_is_shipping_required(
        root: SyncWebhookControlContext[models.Order], info
    ):
        return (
            OrderLinesByOrderIdLoader(info.context)
            .load(root.node.id)
            .then(lambda lines: any(line.is_shipping_required for line in lines))
        )

    @staticmethod
    def resolve_gift_cards(root: SyncWebhookControlContext[models.Order], info):
        return GiftCardsByOrderIdLoader(info.context).load(root.node.id)

    @staticmethod
    def resolve_voucher(root: SyncWebhookControlContext[models.Order], info):
        order = root.node
        if not order.voucher_id:
            return None

        def wrap_voucher_with_channel_context(data):
            voucher, channel = data
            return ChannelContext(node=voucher, channel_slug=channel.slug)

        voucher = VoucherByIdLoader(info.context).load(order.voucher_id)
        channel = ChannelByIdLoader(info.context).load(order.channel_id)

        return Promise.all([voucher, channel]).then(wrap_voucher_with_channel_context)

    @staticmethod
    def resolve_voucher_code(root: SyncWebhookControlContext[models.Order], info):
        if not root.node.voucher_code:
            return None
        return root.node.voucher_code

    @staticmethod
    def resolve_language_code_enum(
        root: SyncWebhookControlContext[models.Order], _info
    ):
        return LanguageCodeEnum[str_to_enum(root.node.language_code)]

    @staticmethod
    def resolve_original(root: SyncWebhookControlContext[models.Order], _info):
        if not root.node.original_id:
            return None
        return graphene.Node.to_global_id("Order", root.node.original_id)

    @staticmethod
    @traced_resolver
    def resolve_errors(root: SyncWebhookControlContext[models.Order], info):
        order = root.node
        if order.status == OrderStatus.DRAFT:

            @allow_writer_in_context(info.context)
            def _validate_order(data):
                lines, manager = data
                country = get_order_country(order)
                database_connection_name = get_database_connection_name(info.context)
                try:
                    validate_draft_order(
                        order=order,
                        lines=lines,
                        country=country,
                        manager=manager,
                        database_connection_name=database_connection_name,
                        allow_sync_webhooks=root.allow_sync_webhooks,
                    )
                except ValidationError as e:
                    return validation_error_to_error_type(e, OrderError)
                return []

            lines = OrderLinesByOrderIdLoader(info.context).load(order.id)
            manager = get_plugin_manager_promise(info.context)
            return Promise.all([lines, manager]).then(_validate_order)

        return []

    @staticmethod
    def resolve_granted_refunds(root: SyncWebhookControlContext[models.Order], info):
        def _wrap_with_sync_webhook_control_context(granted_refunds):
            return [
                SyncWebhookControlContext(
                    granted_refund, allow_sync_webhooks=root.allow_sync_webhooks
                )
                for granted_refund in granted_refunds
            ]

        return (
            OrderGrantedRefundsByOrderIdLoader(info.context)
            .load(root.node.id)
            .then(_wrap_with_sync_webhook_control_context)
        )

    @staticmethod
    def resolve_total_granted_refund(
        root: SyncWebhookControlContext[models.Order], info
    ):
        order = root.node

        def calculate_total_granted_refund(granted_refunds):
            return sum(
                [granted_refund.amount for granted_refund in granted_refunds],
                zero_money(order.currency),
            )

        return (
            OrderGrantedRefundsByOrderIdLoader(info.context)
            .load(order.id)
            .then(calculate_total_granted_refund)
        )

    @staticmethod
    def resolve_total_refunded(root: SyncWebhookControlContext[models.Order], info):
        order = root.node

        def _resolve_total_refunded_for_transactions(transactions):
            return sum(
                [transaction.amount_refunded for transaction in transactions],
                zero_money(order.currency),
            )

        def _resolve_total_refunded_for_payment(transactions):
            # Calculate payment total refund requires iterating
            # over payment's transactions
            total_refund_amount = Decimal(0)
            for transaction in transactions:
                if (
                    transaction.kind == TransactionKind.REFUND
                    and transaction.is_success
                ):
                    total_refund_amount += transaction.amount
            return prices.Money(total_refund_amount, order.currency)

        def _resolve_total_refund(data):
            payments, transactions = data
            last_payment = get_last_payment(payments)
            payment_is_active = last_payment and last_payment.is_active
            payment_is_fully_refunded = (
                last_payment
                and last_payment.charge_status == ChargeStatus.FULLY_REFUNDED
            )

            if payment_is_active or payment_is_fully_refunded:
                return (
                    TransactionByPaymentIdLoader(info.context)
                    .load(last_payment.id)
                    .then(_resolve_total_refunded_for_payment)
                )
            return _resolve_total_refunded_for_transactions(transactions)

        payments = PaymentsByOrderIdLoader(info.context).load(order.id)
        transactions = TransactionItemsByOrderIDLoader(info.context).load(order.id)
        return Promise.all([payments, transactions]).then(_resolve_total_refund)

    @staticmethod
    def resolve_total_refund_pending(
        root: SyncWebhookControlContext[models.Order], info
    ):
        order = root.node

        def _resolve_total_refund_pending(transactions):
            return sum(
                [transaction.amount_refund_pending for transaction in transactions],
                zero_money(order.currency),
            )

        return (
            TransactionItemsByOrderIDLoader(info.context)
            .load(order.id)
            .then(_resolve_total_refund_pending)
        )

    @staticmethod
    def resolve_total_authorize_pending(
        root: SyncWebhookControlContext[models.Order], info
    ):
        order = root.node

        def _resolve_total_authorize_pending(transactions):
            return sum(
                [transaction.amount_authorize_pending for transaction in transactions],
                zero_money(order.currency),
            )

        return (
            TransactionItemsByOrderIDLoader(info.context)
            .load(order.id)
            .then(_resolve_total_authorize_pending)
        )

    @staticmethod
    def resolve_total_charge_pending(
        root: SyncWebhookControlContext[models.Order], info
    ):
        order = root.node

        def _resolve_total_charge_pending(transactions):
            return sum(
                [transaction.amount_charge_pending for transaction in transactions],
                zero_money(order.currency),
            )

        return (
            TransactionItemsByOrderIDLoader(info.context)
            .load(order.id)
            .then(_resolve_total_charge_pending)
        )

    @staticmethod
    def resolve_total_cancel_pending(
        root: SyncWebhookControlContext[models.Order], info
    ):
        order = root.node

        def _resolve_total_cancel_pending(transactions):
            return sum(
                [transaction.amount_cancel_pending for transaction in transactions],
                zero_money(order.currency),
            )

        return (
            TransactionItemsByOrderIDLoader(info.context)
            .load(order.id)
            .then(_resolve_total_cancel_pending)
        )

    @staticmethod
    def resolve_total_remaining_grant(
        root: SyncWebhookControlContext[models.Order], info
    ):
        order = root.node

        def _resolve_total_remaining_grant_for_transactions(
            transactions, total_granted_refund
        ):
            amount_fields = [
                "amount_charged",
                "amount_authorized",
                "amount_refunded",
                "amount_charge_pending",
                "amount_authorize_pending",
                "amount_refund_pending",
            ]
            # Calculate total processed amount, it excluded the cancel amounts
            # as it's the amount that never has been charged
            processed_amount = sum(
                [
                    sum(
                        [getattr(transaction, field) for field in amount_fields],
                        zero_money(order.currency),
                    )
                    for transaction in transactions
                ],
                zero_money(order.currency),
            )
            refunded_amount = sum(
                [
                    transaction.amount_refunded + transaction.amount_refund_pending
                    for transaction in transactions
                ],
                zero_money(order.currency),
            )
            already_granted_refund = max(
                refunded_amount - (processed_amount - order.total.gross),
                zero_money(order.currency),
            )

            return max(
                total_granted_refund - already_granted_refund,
                zero_money(order.currency),
            )

        def _resolve_total_remaining_grant(data):
            transactions, payments, granted_refunds = data
            total_granted_refund = sum(
                [granted_refund.amount for granted_refund in granted_refunds],
                zero_money(order.currency),
            )
            # total_granted_refund cannot be bigger than order.total
            total_granted_refund = min(total_granted_refund, order.total.gross)

            def _resolve_total_remaining_grant_for_payment(payment_transactions):
                total_refund_amount = Decimal(0)
                for transaction in payment_transactions:
                    if transaction.kind == TransactionKind.REFUND:
                        total_refund_amount += transaction.amount
                return prices.Money(
                    total_granted_refund.amount - total_refund_amount, order.currency
                )

            last_payment = get_last_payment(payments)
            if last_payment and last_payment.is_active:
                return (
                    TransactionByPaymentIdLoader(info.context)
                    .load(last_payment.id)
                    .then(_resolve_total_remaining_grant_for_payment)
                )
            return _resolve_total_remaining_grant_for_transactions(
                transactions, total_granted_refund
            )

        granted_refunds = OrderGrantedRefundsByOrderIdLoader(info.context).load(
            order.id
        )
        transactions = TransactionItemsByOrderIDLoader(info.context).load(order.id)
        payments = PaymentsByOrderIdLoader(info.context).load(order.id)
        return Promise.all([transactions, payments, granted_refunds]).then(
            _resolve_total_remaining_grant
        )

    @staticmethod
    def resolve_display_gross_prices(
        root: SyncWebhookControlContext[models.Order], info
    ):
        order = root.node
        tax_config = TaxConfigurationByChannelId(info.context).load(order.channel_id)
        country_code = get_order_country(order)

        def load_tax_country_exceptions(tax_config):
            tax_configs_per_country = (
                TaxConfigurationPerCountryByTaxConfigurationIDLoader(info.context).load(
                    tax_config.id
                )
            )

            def calculate_display_gross_prices(tax_configs_per_country):
                tax_config_country = next(
                    (
                        tc
                        for tc in tax_configs_per_country
                        if tc.country.code == country_code
                    ),
                    None,
                )
                return get_display_gross_prices(tax_config, tax_config_country)

            return tax_configs_per_country.then(calculate_display_gross_prices)

        return tax_config.then(load_tax_country_exceptions)

    @classmethod
    def resolve_shipping_tax_class(
        cls, root: SyncWebhookControlContext[models.Order], info
    ):
        if root.node.shipping_method_id:
            return cls.resolve_shipping_method(root, info).then(
                lambda shipping_method_data: (
                    shipping_method_data.tax_class if shipping_method_data else None
                )
            )
        return None

    @staticmethod
    def resolve_shipping_tax_class_metadata(
        root: SyncWebhookControlContext[models.Order], _info
    ):
        return resolve_metadata(root.node.shipping_tax_class_metadata)

    @staticmethod
    def resolve_shipping_tax_class_private_metadata(
        root: SyncWebhookControlContext[models.Order], info
    ):
        order = root.node
        check_private_metadata_privilege(order, info)
        return resolve_metadata(order.shipping_tax_class_private_metadata)

    @staticmethod
    def resolve_checkout_id(root: SyncWebhookControlContext[models.Order], _info):
        order = root.node
        if order.checkout_token:
            return graphene.Node.to_global_id("Checkout", order.checkout_token)
        return None

    @staticmethod
    def __resolve_references(roots: list["Order"], info):
        requestor = get_user_or_app_from_context(info.context)
        requestor_has_access_to_all = has_one_of_permissions(
            requestor, [OrderPermissions.MANAGE_ORDERS]
        )

        if requestor:
            qs = resolve_orders(
                info,
                requestor_has_access_to_all=requestor_has_access_to_all,
                requesting_user=info.context.user,
            )
        else:
            qs = models.Order.objects.none()

        results: list[SyncWebhookControlContext[models.Order] | None] = []
        for order in resolve_federation_references(Order, roots, qs):
            if not order:
                results.append(None)
                continue
            results.append(SyncWebhookControlContext(order, allow_sync_webhooks=False))
        return results


class OrderCountableConnection(CountableConnection):
    class Meta:
        doc_category = DOC_CATEGORY_ORDERS
        node = Order
