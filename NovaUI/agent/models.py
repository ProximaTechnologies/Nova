from django.db import models
from django.utils import timezone


class CollectorAuth(models.Model):
    node = models.OneToOneField('config.Node', on_delete=models.CASCADE)
    mimir_consumer_name = models.CharField(max_length=255, blank=True)
    loki_consumer_name = models.CharField(max_length=255, blank=True)
    config_consumer_name = models.CharField(max_length=255, blank=True)
    provisioner_backend = models.CharField(max_length=100, blank=True)
    provisioned_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    mimir_password = models.CharField(max_length=255, blank=True, default="")
    loki_password = models.CharField(max_length=255, blank=True, default="")
    tempo_password = models.CharField(max_length=255, blank=True, default="")
    config_password = models.CharField(max_length=255, blank=True, default="")

    def __str__(self):
        return f"{self.node.node_name} credentials"
