declare "metrics" {
    {{ if .Features.SelfMonitor }}
    prometheus.exporter.self "self_exporter_{{ .ID }}" {}
    prometheus.scrape "scrape_self_monitor{{ .ID }}" {
        targets = prometheus.exporter.self.self_exporter_{{.ID}}.targets
        forward_to = [prometheus.remote_write.rw_{{.ID}}.receiver]
    }

    {{ end }}

    {{ if .Features.LinuxMonitor }}
    prometheus.exporter.unix "unix_exporter_{{ .ID }}" {}
    prometheus.scrape "scrape_unix_exporter_{{ .ID }}" {
        targets    = prometheus.exporter.unix.unix_exporter_{{.ID}}.targets
        forward_to = [prometheus.remote_write.rw_{{.ID}}.receiver]
    }

    {{ end }}

    {{ if .Features.WindowsMonitor }}
    prometheus.exporter.windows "windows_exporter_{{ .ID }}" {
        enabled_collectors = ["cpu", "cs", "logical_disk", "net", "os", "service", "system", "time", "diskdrive"]
    }

    // Configure a prometheus.scrape component to collect windows metrics.
    prometheus.scrape "scrape_windows_exporter_{{ .ID }}" {
    targets    = prometheus.exporter.windows.windows_exporter_{{ .ID }}.targets
    forward_to = [prometheus.remote_write.rw_{{ .ID }}.receiver]
    }
    {{end}}

    {{ if .Features.ContainerMonitor }}
        prometheus.exporter.cadvisor "container_exporter_{{ .ID }}" {
            docker_host = "unix:///var/run/docker.sock"
        }
        prometheus.scrape "scrape_container_exporter_{{ .ID }}" {
        targets    = prometheus.exporter.cadvisor.container_exporter_{{ .ID }}.targets
        forward_to = [ prometheus.remote_write.rw_{{ .ID }}.receiver ]
        }
    {{end}}

    {{ if or .Features.LinuxMonitor (or .Features.SelfMonitor (or .Features.WindowsMonitor .Features.ContainerMonitor)) }}
    prometheus.remote_write "rw_{{ .ID }}" {
        endpoint {
            url = "{{.LGTMCfg.Mimir.URL}}"
            basic_auth {
                username = "{{.ID}}-mimir"
                password = "{{.Credentials.mimir_password}}"
            }
        }
    }
    {{end}}
}

declare "logs" {
    {{if .Features.JournalLog}}
        loki.relabel "journal" {
            forward_to = []
            rule {
                source_labels = ["__journal__systemd_unit"]
                target_label  = "unit"
            }
            rule {
                source_labels = ["__journal__boot_id"]
                target_label  = "boot_id"
            }
            rule {
                source_labels = ["__journal__transport"]
                target_label  = "transport"
            }
            rule {
                source_labels = ["__journal_priority_keyword"]
                target_label  = "level"
            }
            rule {
                source_labels = ["__journal__hostname"]
                target_label  = "instance"
            }
        }
        loki.source.journal "read" {
            forward_to = [loki.write.lw_{{ .ID }}.receiver,]
            relabel_rules = loki.relabel.journal.rules
            labels = {
            "job" = "log_exporter",
            }
        }
    {{end}}
    {{ if .Features.WindowsEventLog}}
        loki.source.windowsevent "application_logs{{ .ID }}"  {
        eventlog_name = "Application"
        forward_to = [loki.write.lw_{{ .ID }}.receiver]
        }
    {{end}}


    {{ if .Features.DockerLog}}
        discovery.docker "container_logs_{{ .ID }}" {
        host = "unix:///var/run/docker.sock"
        }

        loki.source.docker "container_logs_{{ .ID }}" {
        host       = "unix:///var/run/docker.sock"
        targets    = discovery.docker.container_logs_{{ .ID }}.targets
        labels     = {"app" = "docker"}
        forward_to = [loki.write.lw_{{ .ID }}.receiver]
        }
    {{end}}


    {{if .Features.FileMonitor}}
        local.file_match "files_{{ .ID }}" {
            path_targets = [
                {__path__ = "/tmp/*.log"},
            ]
        }

        loki.source.file "tmpfiles" {
            targets    = local.file_match.files_{{.ID}}.targets
            forward_to = [loki.write.lw_{{.ID}}.receiver]
        }
    {{end}}

    {{if .Features.JournalLog}}
        loki.write "lw_{{ .ID }}" {
            endpoint {
                url ="{{.LGTMCfg.Loki.URL}}"
                headers = {
                    "X-Scope-OrgID" = "{{.TenantID}}",
                }
                basic_auth {
                    username = "{{.ID}}-loki"
                    password = "{{.Credentials.loki_password}}"
                }
            }
        }
    {{end}}
}

{{ if or .Features.LinuxMonitor (or .Features.SelfMonitor (or .Features.WindowsMonitor .Features.ContainerMonitor)) }}
    metrics {}
{{end}}

{{if .Features.JournalLog}}
    logs {}
{{end}}