# Multi-Tenant Plugin for Saleor

This plugin enables multi-tenancy in Saleor by inspecting the `X-Tenant-Id` header in incoming requests.

## Configuration

To use this plugin, enable it in your Saleor configuration.
No additional configuration is required for the plugin itself, beyond activating it.

## Behavior

When a request is received:

1. The plugin checks for the presence of an `X-Tenant-Id` header.
2. If the header is found, the plugin extracts the tenant ID.
3. The tenant ID is then used to set the tenant context for the request. (Currently, this is a placeholder and only prints the tenant ID).

This allows Saleor to operate in a multi-tenant fashion, isolating data and behavior based on the provided tenant ID.
