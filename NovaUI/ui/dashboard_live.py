import logging

from config.models import Node, NodeStatus

logger = logging.getLogger(__name__)


def dashboard_group_name(tenant_id):
    return f"dashboard_tenant_{tenant_id}"


def _isoformat(value):
    return value.isoformat() if value else None


def dashboard_live_payload_for_tenant_id(tenant_id):
    collectors = list(Node.objects.filter(tenant_id=tenant_id).order_by("node_name"))
    status_counts = {status: 0 for status in NodeStatus.values}
    last_signal_candidates = []
    collector_rows = []

    for collector in collectors:
        status = collector.status
        last_signal_at = collector.effective_last_seen_at
        status_counts[status] += 1
        if last_signal_at:
            last_signal_candidates.append(last_signal_at)

        collector_rows.append({
            "node_id": str(collector.node_id),
            "status": status,
            "status_label": collector.status_label,
            "last_signal_at": _isoformat(last_signal_at),
            "alloy_version": collector.alloy_version,
            "supervisor_version": collector.supervisor_version,
        })

    last_signal_at = max(last_signal_candidates) if last_signal_candidates else None
    nodes_warning = status_counts[NodeStatus.WARNING]
    nodes_offline = status_counts[NodeStatus.OFFLINE]
    nodes_never_connected = status_counts[NodeStatus.NEVER_CONNECTED]

    return {
        "type": "dashboard.status",
        "last_signal_at": _isoformat(last_signal_at),
        "nodes_online": status_counts[NodeStatus.ONLINE],
        "nodes_warning": nodes_warning,
        "nodes_offline": nodes_offline,
        "nodes_never_connected": nodes_never_connected,
        "needs_attention": nodes_warning + nodes_offline + nodes_never_connected,
        "status_counts": status_counts,
        "collectors": collector_rows,
    }


def dashboard_live_payload(tenant):
    return dashboard_live_payload_for_tenant_id(tenant.pk)


def broadcast_dashboard_status(tenant_id):
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    channel_layer = get_channel_layer()
    if channel_layer is None:
        return

    payload = dashboard_live_payload_for_tenant_id(tenant_id)
    try:
        async_to_sync(channel_layer.group_send)(
            dashboard_group_name(tenant_id),
            {
                "type": "dashboard.status",
                "payload": payload,
            },
        )
    except Exception:
        logger.warning("Failed to broadcast dashboard status.", exc_info=True)
