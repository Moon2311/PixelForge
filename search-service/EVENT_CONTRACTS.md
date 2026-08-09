# Product Catalog Domain Events

This document describes the domain events published by the Product Catalog service and consumed by the Search Service.

## Architecture

```
┌─────────────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   Product Catalog API   │────▶│   OutboxEvent    │────▶│  Event Relay    │
│   (PostgreSQL)          │     │   (PostgreSQL)   │     │  (Kafka)        │
└─────────────────────────┘     └──────────────────┘     └─────────────────┘
```

- **Product Catalog API**: Handles CRUD operations on PostgreSQL
- **OutboxEvent**: Stores domain events in the same transaction as data changes
- **Event Relay**: Background process that polls and publishes events to Kafka
- **Search Service**: Consumes events and updates Elasticsearch projection

## Event Types

### Product Events
- `ProductCreated` - New product added to catalog
- `ProductUpdated` - Product details modified
- `ProductDeleted` - Product soft-deleted
- `ProductPublished` - Product status changed to active
- `ProductArchived` - Product status changed to archived

### Category Events
- `CategoryCreated` - New category added
- `CategoryUpdated` - Category details modified
- `CategoryDeleted` - Category soft-deleted

### Subcategory Events
- `SubcategoryCreated` - New subcategory added
- `SubcategoryUpdated` - Subcategory details modified
- `SubcategoryDeleted` - Subcategory soft-deleted

### Brand Events
- `BrandCreated` - New brand added
- `BrandUpdated` - Brand details modified
- `BrandDeleted` - Brand soft-deleted

### Product Image Events
- `ProductImageAdded` - Image added to product
- `ProductImageRemoved` - Image removed from product

### Product Variant Events
- `ProductVariantCreated` - New variant added
- `ProductVariantUpdated` - Variant details modified

### Price Events
- `PriceCreated` - New price set for variant
- `PriceUpdated` - Price modified

### Inventory Events
- `InventoryUpdated` - Stock levels changed

### Review Events
- `ProductReviewCreated` - New review submitted
- `ProductReviewUpdated` - Review modified

## Event Payload Structure

All events follow this envelope structure:

```json
{
  "event_id": "uuid",
  "event_type": "ProductCreated",
  "aggregate_type": "product",
  "aggregate_id": "123",
  "version": 1,
  "occurred_at": "2026-08-09T12:00:00Z",
  "data": {
    "product_id": 123,
    "sku": "PROD-001",
    "name": "iPhone 15",
    "slug": "iphone-15",
    "status": "active",
    "is_featured": true,
    "brand_id": 1,
    "category_id": 2,
    "subcategory_id": 3
  }
}
```

## Kafka Topics

Events are published to topics named `{aggregate_type}`:

- `catalog.product` - Product events
- `catalog.category` - Category events
- `catalog.subcategory` - Subcategory events
- `catalog.brand` - Brand events
- `catalog.product_image` - Image events
- `catalog.product_variant` - Variant events
- `catalog.variant_price` - Price events
- `catalog.inventory` - Inventory events
- `catalog.product_review` - Review events

## Reliability Features

### Transactional Outbox
- Events are written in the same database transaction as domain changes
- If the transaction fails, neither the data nor the event exists
- Guarantees at-least-once delivery

### Exponential Backoff
- Failed events are retried with exponential backoff
- Initial delay: 2 seconds
- Maximum delay: 5 minutes
- Maximum retries: 5

### Dead Letter Queue
- Events that exceed max retries are moved to failed status
- Use management commands to inspect and retry failed events

### Idempotency
- Each event has a unique `event_id`
- Search Service should use `event_id` for deduplication
- Processing the same event twice produces the same result

## Management Commands

### Start Event Relay
```bash
python manage.py relay_events
python manage.py relay_events --batch-size 50 --poll-interval 10
```

### Manage Dead Letters
```bash
# View statistics
python manage.py dead_letter stats

# Retry failed events
python manage.py dead_letter retry --event-id 123
python manage.py dead_letter retry --status failed --limit 100

# Purge old failed events
python manage.py dead_letter purge --older-than 30
python manage.py dead_letter purge --older-than 30 --dry-run

# Reset stuck events
python manage.py dead_letter reset --stuck-minutes 5
```

## Search Service Integration

Search Service should:

1. Subscribe to Kafka topics
2. Validate incoming events using `event_contracts.py`
3. Process events idempotently using `event_id`
4. Update Elasticsearch projection
5. Use event versioning for schema evolution

Example consumer implementation:

```python
from apps.event_contracts import validate_event, EventType

def handle_event(event_data):
    # Validate event structure
    errors = validate_event(event_data)
    if errors:
        logger.error(f"Invalid event: {errors}")
        return
    
    # Check for duplicate (idempotency)
    if event_store.exists(event_data["event_id"]):
        logger.info(f"Duplicate event: {event_data['event_id']}")
        return
    
    # Process based on event type
    event_type = EventType(event_data["event_type"])
    
    if event_type == EventType.PRODUCT_CREATED:
        index_product(event_data["data"])
    elif event_type == EventType.PRODUCT_UPDATED:
        update_product(event_data["data"])
    # ... etc
    
    # Mark as processed
    event_store.add(event_data["event_id"])
```
