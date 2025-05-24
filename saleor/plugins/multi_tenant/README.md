# Multi-Tenant Plugin for Saleor

This plugin enables multi-tenancy in Saleor by inspecting the `X-Tenant-Id` header in incoming requests and switching the database schema accordingly.

## Configuration

To use this plugin, enable it in your Saleor configuration.
No additional configuration is required for the plugin itself, beyond activating it. Tenant-specific configurations are not stored in the database.

## Behavior

When a request is received:

1. The plugin checks for the presence of an `X-Tenant-Id` header.
2. If the header is found, the plugin extracts the tenant ID.
3. The plugin attempts to switch the database connection to the schema corresponding to the tenant ID using `connection.set_schema(tenant_id)`.
4. If the `X-Tenant-Id` header is not found, or if switching the schema fails, the plugin defaults to the `public` schema.
5. After the request has been processed, the plugin ensures the database connection is reset to the `public` schema using `connection.set_schema_to_public()` in the `process_response` method.

This allows Saleor to operate in a multi-tenant fashion, isolating data at the database schema level based on the provided tenant ID.

**Important Considerations:**

*   **Schema Creation:** This plugin assumes that tenant-specific schemas (e.g., `tenant_abc`, `tenant_xyz`) have already been created in your PostgreSQL database. You will need a separate process for managing the creation and migration of these tenant schemas.
*   **Database User Permissions:** The database user configured for Saleor must have the necessary permissions to switch schemas and access objects within those schemas.
*   **Migrations:** Django migrations will typically run on the `public` schema. You will need a strategy for applying migrations to all tenant schemas. Tools like `django-tenants` or custom management commands can help with this.
*   **Shared vs. Tenant-Specific Data:** Carefully consider which data should be in the `public` schema (shared across all tenants) and which should be in tenant-specific schemas.
