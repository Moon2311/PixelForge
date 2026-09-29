"""PostgreSQL search indexes.

Search runs directly on PostgreSQL, so these indexes back its hot paths:

* ``pg_trgm`` + GIN trigram indexes make case-insensitive substring
  matching (``UPPER(x) LIKE '%TERM%'``) an index scan instead of a full
  table scan:
    - ``product_search_trgm``: keyword search over the product's combined
      text (``apps.catalog.models.product_search_text``).
    - ``product_name_trgm``: autocomplete / name matching.
    - ``spec_value_trgm``: keyword matches on specification values.
* ``product_updated_desc_idx``: default ``updated_at DESC`` ordering of
  GET /api/products/.
"""

import django.contrib.postgres.indexes
import django.db.models.functions.comparison
import django.db.models.functions.text
from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        TrigramExtension(),
        migrations.AddIndex(
            model_name='product',
            index=models.Index(fields=['-updated_at'], name='product_updated_desc_idx'),
        ),
        migrations.AddIndex(
            model_name='product',
            index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper(django.db.models.functions.text.Concat(models.F('name'), models.Value(' '), models.F('sku'), models.Value(' '), models.F('short_description'), models.Value(' '), models.F('description'), models.Value(' '), models.F('specifications_text'), models.Value(' '), models.F('color'), models.Value(' '), models.F('size'), models.Value(' '), django.db.models.functions.comparison.Cast('tags', models.TextField()), output_field=models.TextField())), name='gin_trgm_ops'), name='product_search_trgm'),
        ),
        migrations.AddIndex(
            model_name='product',
            index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('name'), name='gin_trgm_ops'), name='product_name_trgm'),
        ),
        migrations.AddIndex(
            model_name='productspecification',
            index=django.contrib.postgres.indexes.GinIndex(django.contrib.postgres.indexes.OpClass(django.db.models.functions.text.Upper('value'), name='gin_trgm_ops'), name='spec_value_trgm'),
        ),
    ]
