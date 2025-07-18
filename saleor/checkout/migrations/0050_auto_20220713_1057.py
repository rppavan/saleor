# Generated by Django 3.2.14 on 2022-07-13 10:57

from decimal import Decimal

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("checkout", "0049_auto_20220621_0850"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="checkout",
            options={
                "ordering": ("-last_change", "pk"),
                "permissions": (
                    ("manage_checkouts", "Manage checkouts"),
                    ("handle_checkouts", "Handle checkouts"),
                    ("handle_taxes", "Handle taxes"),
                ),
            },
        ),
        migrations.AddField(
            model_name="checkout",
            name="price_expiration",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.AddField(
            model_name="checkout",
            name="shipping_price_gross_amount",
            field=models.DecimalField(
                decimal_places=3, default=Decimal(0), max_digits=12
            ),
        ),
        migrations.AddField(
            model_name="checkout",
            name="shipping_price_net_amount",
            field=models.DecimalField(
                decimal_places=3, default=Decimal(0), max_digits=12
            ),
        ),
        migrations.AddField(
            model_name="checkout",
            name="shipping_tax_rate",
            field=models.DecimalField(
                decimal_places=4, default=Decimal("0.0"), max_digits=5
            ),
        ),
        migrations.AddField(
            model_name="checkout",
            name="subtotal_gross_amount",
            field=models.DecimalField(
                decimal_places=3, default=Decimal(0), max_digits=12
            ),
        ),
        migrations.AddField(
            model_name="checkout",
            name="subtotal_net_amount",
            field=models.DecimalField(
                decimal_places=3, default=Decimal(0), max_digits=12
            ),
        ),
        migrations.AddField(
            model_name="checkout",
            name="total_gross_amount",
            field=models.DecimalField(
                decimal_places=3, default=Decimal(0), max_digits=12
            ),
        ),
        migrations.AddField(
            model_name="checkout",
            name="total_net_amount",
            field=models.DecimalField(
                decimal_places=3, default=Decimal(0), max_digits=12
            ),
        ),
        migrations.AddField(
            model_name="checkoutline",
            name="currency",
            field=models.CharField(max_length=3, null=True),
        ),
        migrations.AddField(
            model_name="checkoutline",
            name="tax_rate",
            field=models.DecimalField(
                decimal_places=4, default=Decimal("0.0"), max_digits=5
            ),
        ),
        migrations.AddField(
            model_name="checkoutline",
            name="total_price_gross_amount",
            field=models.DecimalField(
                decimal_places=3, default=Decimal(0), max_digits=12
            ),
        ),
        migrations.AddField(
            model_name="checkoutline",
            name="total_price_net_amount",
            field=models.DecimalField(
                decimal_places=3, default=Decimal(0), max_digits=12
            ),
        ),
    ]
