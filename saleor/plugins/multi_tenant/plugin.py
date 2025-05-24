from django.db import connection
from saleor.plugins.base_plugin import BasePlugin, PluginConfigurationType
from django.http import HttpRequest, HttpResponse

# Store the original schema to reset it after the request
DEFAULT_SCHEMA = "public"

class MultiTenantPlugin(BasePlugin):
    PLUGIN_ID = "saleor.multi_tenant"
    PLUGIN_NAME = "Multi Tenant"
    PLUGIN_DESCRIPTION = "Plugin to handle multi-tenancy via X-Tenant-Id header and database schema switching."
    CONFIGURATION_PER_CHANNEL = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active = True  # Assuming the plugin is active by default

    def _get_tenant_id(self, request: HttpRequest):
        return request.headers.get("X-Tenant-Id")

    def process_request(self, request: HttpRequest, previous_value):
        tenant_id = self._get_tenant_id(request)
        if tenant_id:
            try:
                connection.set_schema(tenant_id)
                print(f"Switched to schema: {tenant_id}")
            except Exception as e:
                # Handle cases where the schema might not exist or other DB errors
                print(f"Error setting schema {tenant_id}: {e}")
                # Optionally, fall back to default schema or raise an error
                connection.set_schema_to_public()
        else:
            # Ensure we are on the public schema if no tenant ID is provided
            connection.set_schema_to_public()
        return previous_value

    def process_response(self, response: HttpResponse, request: HttpRequest, previous_value):
        # Always reset to the default schema after the request is processed
        connection.set_schema_to_public()
        print(f"Reset schema to public")
        return previous_value

    @classmethod
    def validate_plugin_configuration(
        cls, plugin_configuration: "PluginConfigurationType", **kwargs
    ):
        """Validate if provided configuration is correct."""
        # This plugin does not require specific configuration beyond activation.
        # Configurations are not stored in the DB for tenants.
        if not plugin_configuration.active:
            return
        pass
