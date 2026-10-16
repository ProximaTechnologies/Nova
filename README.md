# Nova
### Observability, Simplified. At Scale.
Nova is a centralized, multi-tenant control plane for the Grafana LGTM observability stack. It simplifies the deployment, configuration, and lifecycle management of observability infrastructure, from agents to dashboards, through a single self-service interface.
Built for scale, Nova eliminates the operational complexity of managing distributed observability deployments. Whether you're managing a handful of servers or thousands of agents across multiple tenants, Nova provides a consistent, automated, and centrally managed experience.
Features
- One-Click Deployment — Deploy and onboard new agents with a single command. No complex setup or manual configuration required.
- Centralized Configuration Management — Manage and distribute agent configurations remotely, without manually modifying individual hosts.
- Self-Service Portal — Empower teams to manage their observability environments through an intuitive web interface.
- Multi-Tenant by Design — Securely manage multiple tenants within a single platform, with isolated configurations and resources.
- Tenant-Level Resource Controls — Define and enforce per-tenant limits to maintain predictable resource consumption.
- Automated Dashboard Provisioning — Automatically deploy preconfigured Grafana dashboards for a consistent, ready-to-use monitoring experience.
- Managed Infrastructure — Streamline the provisioning and lifecycle management of the underlying observability stack.
- Audit Logging (Coming Soon) — Track configuration changes and administrative actions for improved visibility and accountability.
### Architecture
Engineered for Scale. Designed for Simplicity.
Nova is built on a distributed architecture designed to support thousands of agents while maintaining operational efficiency and horizontal scalability.
The platform consists of three core components:
1. Nova UI — The Control Center
A centralized self-service interface for managing tenants, agent configurations, and observability resources. The UI abstracts away the complexity of the underlying infrastructure, allowing teams to focus on observability rather than configuration management.
2. Configuration Server — The Control Plane
The configuration server translates user-defined settings into agent-ready configurations.
Rather than maintaining persistent connections to every agent, Nova uses Redis as a shared state and caching layer. Desired configurations are stored centrally, rendered on demand when requested by an agent, and cached for subsequent requests.
This architecture enables configuration servers to scale horizontally, supporting large agent fleets without requiring dedicated configuration management infrastructure per tenant.
3. Nova Supervisor — The Host-Side Lifecycle Manager
A lightweight supervisor runs alongside Grafana Alloy on each managed host, handling agent installation and version management.
Alloy retrieves its configuration directly from Nova using Grafana's remote configuration protocol, enabling centralized configuration management without manual intervention.
The supervisor also supports optional automatic upgrades. Elevated privileges are only required when performing operations that need them, giving operators flexibility over how agents are maintained.
[Architecture Diagram]
### Why Nova?
Traditional observability deployments often require significant effort to configure, maintain, and scale. As environments grow, managing agent configurations, tenant isolation, dashboards, and infrastructure becomes increasingly complex.
Nova turns observability management into a service.
By consolidating deployment, configuration, and infrastructure management into a single control plane, Nova helps organizations:
- Reduce operational overhead through centralized management and automation.
- Accelerate onboarding with streamlined agent deployment and dashboard provisioning.
- Scale confidently with an architecture designed for large, distributed environments.
- Enable team autonomy through self-service capabilities and tenant-level controls.
- Maintain consistency across observability deployments without sacrificing flexibility.  
One platform. Thousands of agents. Full control.

If you prefer to read a non-ai ramblings of an engineer [readme-no-ai.md](readme-no-ai.md)
