# Nova
### What is Nova ?
Nova is a highly opinionated multi-tenant control plane for Grafana's LGTM observability stack. Nova controls the entire stack from the agent to the dashboard via configurations on the UI. It also

### Features
- 1 click install (curl | bash)
- remote configuration management.
- self service UI
- multi-tenant
- per teneant limits
- auto install dashboard
- managed infra
- auditable -> soon lol.

### Architecture
Nova was designed to be a SaaS service supporting multiple thousands of agents. This is build using 3 Core components, on the server side is the user system consisting of a UI and a CFG server. On the client side a supervisor binary.
The communication between both systems are done via redis. A desired state is written into redis and rendered on call from the alloy agent. The rendered config is then cached back into redis. This allows the CFG server to be horizontally scaled out. 
[Pretend there's an image of the architecture here]

On the host side a small supervisor agent is installed to manage alloy's version. Auto upgrade requires sudo but is optional. alloy configures itself from the remotecfg protocol.
