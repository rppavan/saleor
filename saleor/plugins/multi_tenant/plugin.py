from saleor.plugins.base_plugin import BasePlugin, PluginConfigurationType
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpRequest

class MultiTenantPlugin(BasePlugin):
    PLUGIN_ID = "saleor.multi_tenant"
    PLUGIN_NAME = "Multi Tenant"
    PLUGIN_DESCRIPTION = "Plugin to handle multi-tenancy via X-Tenant-Id header"
    CONFIGURATION_PER_CHANNEL = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.active = True  # Assuming the plugin is active by default

    def _get_tenant_id(self, request: HttpRequest):
        return request.headers.get("X-Tenant-Id")

    def process_request(self, request: HttpRequest, previous_value):
        tenant_id = self._get_tenant_id(request)
        if tenant_id:
            # Here you would typically set the tenant context,
            # for example, by setting a thread-local variable
            # or by modifying the database connection.
            # For this example, we'll just print it.
            print(f"Processing request for tenant: {tenant_id}")
        return previous_value

    @classmethod
    def validate_plugin_configuration(
        cls, plugin_configuration: "PluginConfigurationType", **kwargs
    ):
        """Validate if provided configuration is correct."""
        if not plugin_configuration.active:
            return

        # Add any validation logic for plugin configuration here
        # For this plugin, no specific configuration is needed beyond activation.
        pass
