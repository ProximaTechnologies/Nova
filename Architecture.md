## Architecture
### User Interface
APISIX ingress -> NovaUI

### Configuration path
NovaUI -> Dragonfly(Redis) -> NovaCFG

### Alloy config path
Alloy -> APISIX Ingress -> Basic Auth -> NovaCFG -> Dragonfly
Dragonfly stores prerendered auto expiring configurations for NovaCFG

### Data path
Alloy -> APISIX Ingress -> Basic auth -> Mimir/Loki

Each Agent has a unique basic auth user/password combination for mimir and loki.

