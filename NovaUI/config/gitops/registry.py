from django.conf import settings
from django.utils.module_loading import import_string


def load_plugins(names=None):
    plugin_paths = names or settings.GITOPS_PLUGINS
    plugins = []
    for plugin_path in plugin_paths:
        plugin_cls = import_string(plugin_path)
        plugins.append(plugin_cls())
    return plugins


def filter_plugins(plugins, selected_names=None):
    if not selected_names:
        return plugins
    selected = set(selected_names)
    return [plugin for plugin in plugins if plugin.name in selected]
